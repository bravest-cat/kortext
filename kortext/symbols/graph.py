"""
Deterministic Symbol & Cross-File Reference Graph in FalkorDB.

Maintains a graph of:
  (:Symbol {name})
  (:File {path})
  (s:Symbol)-[:DEFINED_IN {kind, line, signature, docstring, parent}]->(f:File)
  (f:File)-[:IMPORTS {module, line}]->(s:Symbol)
  (f:File)-[:REFERENCES {line, kind}]->(s:Symbol)

Provides sub-millisecond O(1) "Jump to Definition", "Find All References",
and cross-file "Impact Analysis" without requiring a heavy LSP compiler daemon.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import redis

from kortext.falkordb_client import (
    FalkorDBClient,
    FALKORDB_HOST,
    FALKORDB_PORT,
    FALKORDB_PASS,
    get_shared_falkordb_client,
)
from kortext.symbols.formatter import format_impact_report

logger = logging.getLogger("kortext.symbols.graph")

SYMBOLS_GRAPH_NAME = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))


from kortext.symbols.graph_traversal import SymbolGraphTraversalMixin


class SymbolGraphManager(SymbolGraphTraversalMixin):
    """Manages symbol declarations, cross-file imports, and usages in FalkorDB."""

    def __init__(
        self,
        host: str = FALKORDB_HOST,
        port: int = FALKORDB_PORT,
        password: Optional[str] = FALKORDB_PASS,
        graph_name: str = SYMBOLS_GRAPH_NAME,
        client: Optional[FalkorDBClient] = None,
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.graph_name = graph_name
        self.client = client if client is not None else get_shared_falkordb_client(host=self.host, port=self.port, password=self.password)
        self._indexes_ensured = False

    def _ensure_indexes(self):
        """Ensures B-tree schema indexes on :Symbol(name), :Symbol(qualified_name), :File(path), :Document(path), :Tag(name), :Concept(name)."""
        self.client.ensure_indexes(
            self.graph_name,
            [
                ("Symbol", "name"),
                ("Symbol", "qualified_name"),
                ("Symbol", "file_path"),
                ("File", "path"),
                ("Document", "path"),
                ("Tag", "name"),
                ("Concept", "name"),
            ],
        )

    def _query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[List[Any]]:
        """Executes a Cypher query on the symbols graph with token-safe parameter binding and reconnect."""
        if not getattr(self, "_indexes_ensured", False):
            self._ensure_indexes()
            self._indexes_ensured = True
        return self.client.query(self.graph_name, cypher, params)

    def save_file_ast(
        self,
        file_path: str,
        declarations: List[Dict[str, Any]],
        imports: List[Dict[str, Any]],
        references: List[Dict[str, Any]],
    ) -> None:
        """
        Atomically saves a file's declarations, imports, and references using batch UNWIND queries.
        Uses qualified_name identity for declarations so identically named functions/methods across files never collide.
        """
        file_path = str(file_path).replace("\\", "/")
        # Collect candidate symbols defined in this file before detaching definitions
        candidate_syms = set()
        res_defs = self._query(
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {path: $path}) RETURN DISTINCT s.name",
            {"path": file_path},
        )
        if res_defs and len(res_defs) > 1 and isinstance(res_defs[1], list):
            for row in res_defs[1]:
                if row and row[0]:
                    candidate_syms.add(str(row[0]))

        res_fp = self._query(
            "MATCH (s:Symbol {file_path: $path}) RETURN DISTINCT s.name",
            {"path": file_path},
        )
        if res_fp and len(res_fp) > 1 and isinstance(res_fp[1], list):
            for row in res_fp[1]:
                if row and row[0]:
                    candidate_syms.add(str(row[0]))

        # 1. Purge old relationships connected to this file and ensure File node
        self._query(
            "MERGE (f:File {path: $path}) "
            "WITH f "
            "OPTIONAL MATCH (f)<-[:DEFINED_IN]-(c:Symbol)-[r4:CALLS]->() DELETE r4 "
            "WITH f "
            "OPTIONAL MATCH ()-[r5:CALLS {file_path: $path}]->() DELETE r5 "
            "WITH f "
            "OPTIONAL MATCH (f)<-[r1:DEFINED_IN]-() DELETE r1 "
            "WITH f "
            "OPTIONAL MATCH (f)-[r2:IMPORTS]->() DELETE r2 "
            "WITH f "
            "OPTIONAL MATCH (f)-[r3:REFERENCES]->() DELETE r3",
            {"path": file_path},
        )

        # 2. Batch add declarations keyed by deterministic qualified_name
        clean_decls = []
        decl_qname_by_short: Dict[str, str] = {}
        for decl in declarations:
            name = decl.get("name")
            if not name or name == "anonymous":
                continue
            name_str = str(name)
            parent_str = str(decl.get("parent") or "")
            qname = str(
                decl.get("qualified_name")
                or f"{file_path}::{parent_str + '.' if parent_str else ''}{name_str}"
            ).replace("\\", "/")
            decl_qname_by_short.setdefault(name_str, qname)
            if parent_str:
                decl_qname_by_short.setdefault(f"{parent_str}.{name_str}", qname)
            clean_decls.append({
                "name": name_str,
                "qualified_name": qname,
                "kind": str(decl.get("kind", "symbol")),
                "line": int(decl.get("line", 1)),
                "end_line": int(decl.get("end_line", decl.get("line", 1))),
                "signature": str(decl.get("signature") or "")[:200],
                "docstring": str(decl.get("docstring") or "")[:200],
                "parent": parent_str,
            })

        if clean_decls:
            decl_cypher = (
                "MATCH (f:File {path: $path}) "
                "WITH f "
                "UNWIND $declarations AS d "
                "MERGE (s:Symbol {qualified_name: d.qualified_name}) "
                "SET s.name = d.name, s.kind = d.kind, s.file_path = $path, s.parent = d.parent "
                "MERGE (s)-[:DEFINED_IN {kind: d.kind, line: d.line, end_line: d.end_line, signature: d.signature, docstring: d.docstring, parent: d.parent}]->(f)"
            )
            self._query(decl_cypher, {"path": file_path, "declarations": clean_decls})

            # Reconcile pre-existing unresolved or module-scoped stubs matching this declaration
            reconcile_cypher = (
                "UNWIND $declarations AS d "
                "MATCH (s:Symbol {qualified_name: d.qualified_name}) "
                "MATCH (stub:Symbol {name: d.name}) "
                "WHERE id(stub) <> id(s) AND ("
                "  stub.qualified_name IS NULL "
                "  OR stub.qualified_name = d.name "
                "  OR stub.qualified_name STARTS WITH 'unresolved::' "
                "  OR (NOT stub.qualified_name CONTAINS '/' AND NOT stub.qualified_name CONTAINS '\\\\' AND stub.qualified_name ENDS WITH ('::' + d.name))"
                ") "
                "OPTIONAL MATCH (in_f:File)-[imp:IMPORTS]->(stub) "
                "FOREACH (_ IN CASE WHEN in_f IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (in_f)-[:IMPORTS {module: coalesce(imp.module, ''), line: coalesce(imp.line, 1)}]->(s) "
                "  DELETE imp"
                ") "
                "WITH s, stub "
                "OPTIONAL MATCH (ref_f:File)-[ref:REFERENCES]->(stub) "
                "FOREACH (_ IN CASE WHEN ref_f IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (ref_f)-[:REFERENCES {line: coalesce(ref.line, 1), kind: coalesce(ref.kind, 'call'), caller: coalesce(ref.caller, '')}]->(s) "
                "  DELETE ref"
                ") "
                "WITH s, stub "
                "OPTIONAL MATCH (caller_sym:Symbol)-[call_rel:CALLS]->(stub) "
                "FOREACH (_ IN CASE WHEN caller_sym IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (caller_sym)-[:CALLS {line: coalesce(call_rel.line, 1), file_path: coalesce(call_rel.file_path, '')}]->(s) "
                "  DELETE call_rel"
                ") "
                "WITH stub "
                "OPTIONAL MATCH (stub)-[def_rel:DEFINED_IN]->() "
                "WITH stub, count(def_rel) AS def_cnt "
                "WHERE def_cnt = 0 "
                "DETACH DELETE stub"
            )
            self._query(reconcile_cypher, {"declarations": clean_decls})

        # 3. Batch add imports (resolve against existing defined symbols or module-qualified stubs)
        clean_imps = []
        imp_qname_by_short: Dict[str, str] = {}
        for imp in imports:
            sym = imp.get("symbol") or imp.get("imported_name")
            if not sym:
                continue
            sym_str = str(sym)
            mod_str = str(imp.get("module") or "")
            mod_suffix = mod_str.replace(".", "/").lstrip("/") if mod_str else ""
            imp_qname = decl_qname_by_short.get(
                sym_str,
                f"{mod_str}::{sym_str}" if mod_str else f"unresolved::{file_path}::{sym_str}",
            )
            imp_qname_by_short.setdefault(sym_str, imp_qname)
            clean_imps.append({
                "name": sym_str,
                "qualified_name": imp_qname,
                "module": mod_str,
                "mod_suffix": mod_suffix,
                "line": int(imp.get("line", 1)),
            })

        if clean_imps:
            imp_cypher = (
                "MATCH (f:File {path: $path}) "
                "WITH f "
                "UNWIND $imports AS i "
                "OPTIONAL MATCH (existing:Symbol {name: i.name})-[:DEFINED_IN]->(ef:File) "
                "WHERE i.mod_suffix <> '' AND (ef.path CONTAINS i.mod_suffix) "
                "WITH f, i, coalesce(existing.qualified_name, i.qualified_name) AS resolved_qname "
                "MERGE (s:Symbol {qualified_name: resolved_qname}) "
                "SET s.name = coalesce(s.name, i.name) "
                "MERGE (f)-[:IMPORTS {module: i.module, line: i.line}]->(s)"
            )
            self._query(imp_cypher, {"path": file_path, "imports": clean_imps})

        # 4. Batch add references (deduplicated) and symbol calls
        seen_refs = set()
        clean_refs = []
        for ref in references:
            sym = ref.get("symbol") or ref.get("name")
            line = int(ref.get("line", 1))
            caller = str(ref.get("caller") or "")
            if not sym or (sym, line, caller) in seen_refs:
                continue
            seen_refs.add((sym, line, caller))
            sym_str = str(sym)
            caller_qname = decl_qname_by_short.get(caller, f"{file_path}::{caller}") if caller else ""
            caller_short = caller.split(".")[-1] if caller else ""
            target_qname = (
                decl_qname_by_short.get(sym_str)
                or imp_qname_by_short.get(sym_str)
                or f"unresolved::{file_path}::{sym_str}"
            )
            is_local_or_imp = sym_str in decl_qname_by_short or sym_str in imp_qname_by_short
            clean_refs.append({
                "name": sym_str,
                "target_qualified": target_qname,
                "is_local_or_imp": is_local_or_imp,
                "kind": str(ref.get("kind", "call")),
                "line": line,
                "caller": caller_short,
                "caller_full": caller,
                "caller_qualified": caller_qname,
            })

        if clean_refs:
            ref_cypher = (
                "MATCH (f:File {path: $path}) "
                "WITH f "
                "UNWIND $references AS r "
                "OPTIONAL MATCH (existing:Symbol {name: r.name})-[:DEFINED_IN]->() "
                "WITH f, r, collect(DISTINCT existing.qualified_name) AS defs "
                # An unqualified call binds to a definition only when the name is unambiguous;
                # otherwise it gets an unresolved stub instead of a CALLS edge to every homonym.
                "WITH f, r, CASE "
                "  WHEN r.is_local_or_imp THEN r.target_qualified "
                "  WHEN size(defs) = 1 THEN defs[0] "
                "  ELSE r.target_qualified "
                "END AS final_qname "
                "MERGE (s:Symbol {qualified_name: final_qname}) "
                "SET s.name = coalesce(s.name, r.name) "
                "MERGE (f)-[:REFERENCES {line: r.line, kind: r.kind, caller: r.caller}]->(s) "
                "FOREACH (_ IN CASE WHEN r.caller <> '' THEN [1] ELSE [] END | "
                "  MERGE (c:Symbol {qualified_name: r.caller_qualified}) "
                "  SET c.name = r.caller, c.file_path = $path "
                "  MERGE (c)-[:CALLS {line: r.line, file_path: $path}]->(s)"
                ")"
            )
            batch_size = 100
            for i in range(0, len(clean_refs), batch_size):
                batch = clean_refs[i:i + batch_size]
                self._query(ref_cypher, {"path": file_path, "references": batch})

        # 5. Remove orphaned Symbol nodes whose definitions and active references were removed
        if candidate_syms:
            self._query(
                "UNWIND $names AS name "
                "MATCH (s:Symbol {name: name}) "
                "WHERE NOT (s)-[:DEFINED_IN]->(:File) AND NOT ()-[:IMPORTS|REFERENCES|CALLS]->(s) "
                "DETACH DELETE s",
                {"names": list(candidate_syms)},
            )

    def save_markdown_ast(
        self,
        file_path: str,
        title: str,
        frontmatter: Dict[str, Any],
        sections: List[Dict[str, Any]],
        declarations: List[Dict[str, Any]],
        links: List[Dict[str, Any]],
        tags: List[str],
    ) -> None:
        """
        Atomically saves a Markdown document's structural sections, headings (as Section nodes),
        inter-document wiki-links, standard markdown links, and tags into FalkorDB.
        """
        file_path = str(file_path).replace("\\", "/")
        safe_title = str(title or Path(file_path).stem)

        # 1. Ensure File:Document node and purge previous markdown relationships
        self._query(
            "MERGE (f:File {path: $path}) "
            "SET f:Document, f.title = $title, f.is_markdown = true "
            "WITH f "
            "OPTIONAL MATCH (f)<-[r1:DEFINED_IN]-(old_s:Symbol) DELETE r1 "
            "WITH f "
            "OPTIONAL MATCH (f)-[r2:CONTAINS_SECTION]->(sec:Section) DETACH DELETE sec "
            "WITH f "
            "OPTIONAL MATCH (f)-[r3:LINKS_TO]->() DELETE r3 "
            "WITH f "
            "OPTIONAL MATCH (f)-[r4:HAS_TAG]->() DELETE r4",
            {"path": file_path, "title": safe_title},
        )

        # 2. Batch add sections (deduplicated against declarations)
        seen_section_keys = set()
        if sections:
            sec_items = []
            for s in sections:
                sec_id = str(s.get("id", "")).replace("\\", "/")
                sec_name = str(s.get("name", ""))
                sec_line = int(s.get("line", 1))
                seen_section_keys.add((sec_name, sec_line))
                if sec_id:
                    seen_section_keys.add((sec_id, 0))
                sec_items.append({
                    "id": sec_id or f"{file_path}#{sec_name}",
                    "name": sec_name,
                    "level": int(s.get("level", 1)),
                    "line": sec_line,
                    "heading_path": str(s.get("heading_path", "")),
                })
            self._query(
                "MATCH (f:File {path: $path}) "
                "WITH f "
                "UNWIND $sections AS s "
                "MERGE (sec:Section {id: s.id}) "
                "SET sec.name = s.name, sec.level = s.level, sec.line = s.line, sec.heading_path = s.heading_path, sec.path = $path "
                "MERGE (f)-[:CONTAINS_SECTION]->(sec)",
                {"path": file_path, "sections": sec_items},
            )

        # 3. Batch add any non-duplicate declarations as Section nodes (isolating code Symbol graph from markdown documents)
        if declarations:
            clean_decls = []
            for decl in declarations:
                name = decl.get("name")
                if not name:
                    continue
                name_str = str(name)
                line_num = int(decl.get("line", 1))
                if (name_str, line_num) in seen_section_keys:
                    continue
                seen_section_keys.add((name_str, line_num))
                clean_decls.append({
                    "name": name_str,
                    "kind": str(decl.get("kind", "section")),
                    "line": line_num,
                    "signature": str(decl.get("signature") or "")[:200],
                    "docstring": str(decl.get("docstring") or "")[:200],
                    "parent": str(decl.get("parent") or ""),
                })
            if clean_decls:
                self._query(
                    "MATCH (f:File {path: $path}) "
                    "WITH f "
                    "UNWIND $declarations AS d "
                    "MERGE (sec:Section {id: $path + '#' + d.name}) "
                    "SET sec.name = d.name, sec.kind = d.kind, sec.line = d.line, sec.signature = d.signature, sec.docstring = d.docstring, sec.parent = d.parent "
                    "MERGE (f)-[:CONTAINS_SECTION]->(sec)",
                    {"path": file_path, "declarations": clean_decls},
                )

        # 4. Batch add links (split into resolved File targets and unresolved Concept targets)
        if links:
            resolved_links = []
            concept_links = []
            for l in links:
                if l.get("is_resolved") and l.get("target_path"):
                    resolved_links.append({
                        "path": str(l["target_path"]).replace("\\", "/"),
                        "label": str(l.get("label", "")),
                        "line": int(l.get("line", 1)),
                        "type": str(l.get("link_type", "markdown_link")),
                        "raw": str(l.get("raw_target", "")),
                    })
                else:
                    target_name = l.get("target_name") or l.get("raw_target", "")
                    if target_name:
                        concept_links.append({
                            "name": str(target_name),
                            "label": str(l.get("label", "")),
                            "line": int(l.get("line", 1)),
                            "type": str(l.get("link_type", "wiki_link")),
                            "raw": str(l.get("raw_target", "")),
                        })

            if resolved_links:
                self._query(
                    "MATCH (f:File {path: $path}) "
                    "WITH f "
                    "UNWIND $links AS r "
                    "MERGE (t:File {path: r.path}) "
                    "MERGE (f)-[:LINKS_TO {label: r.label, line: r.line, link_type: r.type, raw_target: r.raw}]->(t)",
                    {"path": file_path, "links": resolved_links},
                )

            if concept_links:
                self._query(
                    "MATCH (f:File {path: $path}) "
                    "WITH f "
                    "UNWIND $links AS c "
                    "MERGE (t:Concept {name: c.name}) "
                    "MERGE (f)-[:LINKS_TO {label: c.label, line: c.line, link_type: c.type, raw_target: c.raw}]->(t)",
                    {"path": file_path, "links": concept_links},
                )

        # 5. Batch add tags
        if tags:
            tag_items = [{"name": str(t)} for t in tags if t]
            if tag_items:
                self._query(
                    "MATCH (f:File {path: $path}) "
                    "WITH f "
                    "UNWIND $tags AS t "
                    "MERGE (tag:Tag {name: t.name}) "
                    "MERGE (f)-[:HAS_TAG]->(tag)",
                    {"path": file_path, "tags": tag_items},
                )

    def delete_file_ast(self, file_path: str) -> None:
        """
        Completely purges a file and its associated AST entities from FalkorDB:
        - Removes the File node
        - Removes associated Section nodes
        - Removes ghost CALLS edges originating from this file
        - Removes orphaned Symbol nodes whose only definition was in this file
        """
        file_path = str(file_path).replace("\\", "/")
        # 1. Identify candidate symbols defined in this file (or with s.file_path = $path)
        # before relationships to the File node are removed
        candidate_syms = set()
        res_defs = self._query(
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {path: $path}) RETURN DISTINCT s.name",
            {"path": file_path},
        )
        if res_defs and len(res_defs) > 1 and isinstance(res_defs[1], list):
            for row in res_defs[1]:
                if row and row[0]:
                    candidate_syms.add(str(row[0]))

        res_fp = self._query(
            "MATCH (s:Symbol {file_path: $path}) RETURN DISTINCT s.name",
            {"path": file_path},
        )
        if res_fp and len(res_fp) > 1 and isinstance(res_fp[1], list):
            for row in res_fp[1]:
                if row and row[0]:
                    candidate_syms.add(str(row[0]))

        # Unresolved-reference stubs this file pointed at: check them for orphaning in step 5
        for stub_q in (
            "MATCH (:File {path: $path})-[:IMPORTS|REFERENCES]->(s:Symbol) RETURN DISTINCT s.name",
            "MATCH ()-[:CALLS {file_path: $path}]->(s:Symbol) RETURN DISTINCT s.name",
        ):
            res_stub = self._query(stub_q, {"path": file_path})
            if res_stub and len(res_stub) > 1 and isinstance(res_stub[1], list):
                for row in res_stub[1]:
                    if row and row[0]:
                        candidate_syms.add(str(row[0]))

        # 2. Detach delete associated Section nodes connected to File
        self._query(
            "OPTIONAL MATCH (f:File {path: $path})-[:CONTAINS_SECTION]->(sec:Section) DETACH DELETE sec",
            {"path": file_path},
        )

        # 3. Purge ghost CALLS edges originating from this file
        self._query(
            "OPTIONAL MATCH ()-[r:CALLS {file_path: $path}]->() DELETE r",
            {"path": file_path},
        )

        # 4. Detach delete the File node itself
        self._query(
            "MATCH (f:File {path: $path}) DETACH DELETE f",
            {"path": file_path},
        )

        # 5. Remove orphaned Symbol nodes whose only definition was in this file
        if candidate_syms:
            self._query(
                "UNWIND $names AS name "
                "MATCH (s:Symbol {name: name}) "
                "WHERE NOT (s)-[:DEFINED_IN]->(:File) AND NOT ()-[:IMPORTS|REFERENCES|CALLS]->(s) "
                "DETACH DELETE s",
                {"names": list(candidate_syms)},
            )

    def remove_file(self, file_path: str) -> None:
        """Purges a deleted file and its symbol relationships from the graph."""
        self.delete_file_ast(file_path)

    def clear(self) -> None:
        """Deletes all nodes and relationships in this symbol graph."""
        self._query("MATCH (n) DETACH DELETE n")

    def find_definition(self, symbol_name: str) -> List[Dict[str, Any]]:
        """Finds all exact definition sites of a symbol across indexed files (code symbols & markdown documents)."""
        clean_sym = symbol_name.strip()
        res = self._query(
            "MATCH (s:Symbol)-[r:DEFINED_IN]->(f:File) "
            "WHERE s.name = $name "
            "RETURN s.name, r.kind, r.line, r.signature, r.docstring, r.parent, f.path",
            {"name": clean_sym},
        )
        if not (res and len(res) > 1 and res[1]):
            res = self._query(
                "MATCH (s:Symbol)-[r:DEFINED_IN]->(f:File) "
                "WHERE toLower(s.name) = toLower($name) "
                "RETURN s.name, r.kind, r.line, r.signature, r.docstring, r.parent, f.path",
                {"name": clean_sym},
            )
        definitions = []
        if res and len(res) > 1 and isinstance(res[1], list):
            for row in res[1]:
                if len(row) >= 7:
                    definitions.append({
                        "name": str(row[0]),
                        "kind": str(row[1]),
                        "line": int(row[2]) if str(row[2]).isdigit() else 1,
                        "signature": str(row[3]),
                        "docstring": str(row[4]),
                        "parent": str(row[5]) if row[5] else None,
                        "file_path": str(row[6]),
                    })

        # Also search Document nodes (by title or ending path)
        clean_name = symbol_name.strip()
        doc_res = self._query(
            "MATCH (d:Document) "
            "WHERE toLower(d.title) = toLower($name) OR toLower(d.path) ENDS WITH toLower($name) "
            "RETURN coalesce(d.title, d.path) AS title, 'document' AS kind, 1 AS line, d.path AS signature, d.path AS doc_path",
            {"name": clean_name},
        )
        if doc_res and len(doc_res) > 1 and isinstance(doc_res[1], list):
            for row in doc_res[1]:
                if len(row) >= 5:
                    definitions.append({
                        "name": str(row[0]),
                        "kind": "document",
                        "line": 1,
                        "signature": f"Document: {row[0]}",
                        "docstring": "Markdown knowledge note",
                        "parent": None,
                        "file_path": str(row[4]),
                    })

        return definitions

    def find_references(self, symbol_name: str) -> Dict[str, Any]:
        """
        Finds definitions, imports, reference call sites, and document backlinks for a symbol across files.
        """
        clean_name = symbol_name.strip()
        definitions = self.find_definition(clean_name)

        # 1. Files importing this symbol
        imports_res = self._query(
            "MATCH (f:File)-[r:IMPORTS]->(s:Symbol) "
            "WHERE toLower(s.name) = toLower($name) "
            "RETURN f.path, r.module, r.line",
            {"name": clean_name},
        )
        imports = []
        if imports_res and len(imports_res) > 1 and isinstance(imports_res[1], list):
            for row in imports_res[1]:
                if len(row) >= 3:
                    imports.append({
                        "file_path": str(row[0]),
                        "module": str(row[1]),
                        "line": int(row[2]) if str(row[2]).isdigit() else 1,
                    })

        # 2. Files referencing/calling this symbol
        refs_res = self._query(
            "MATCH (f:File)-[r:REFERENCES]->(s:Symbol) "
            "WHERE toLower(s.name) = toLower($name) "
            "RETURN f.path, r.line, r.kind",
            {"name": clean_name},
        )
        references = []
        if refs_res and len(refs_res) > 1 and isinstance(refs_res[1], list):
            for row in refs_res[1]:
                if len(row) >= 3:
                    references.append({
                        "file_path": str(row[0]),
                        "line": int(row[1]) if str(row[1]).isdigit() else 1,
                        "kind": str(row[2]),
                    })

        # 3. Documents linking to this symbol, file, or concept (Backlinks)
        doc_links_res = self._query(
            "MATCH (caller:Document)-[r:LINKS_TO]->(target) "
            "WHERE (target:File AND toLower(target.path) ENDS WITH toLower($name)) "
            "   OR (target:Document AND toLower(target.title) = toLower($name)) "
            "   OR (target:Concept AND toLower(target.name) = toLower($name)) "
            "   OR (target:Symbol AND toLower(target.name) = toLower($name)) "
            "RETURN caller.path, coalesce(caller.title, caller.path), coalesce(r.label, 'link'), r.line, r.link_type",
            {"name": clean_name},
        )
        backlinks = []
        if doc_links_res and len(doc_links_res) > 1 and isinstance(doc_links_res[1], list):
            for row in doc_links_res[1]:
                if len(row) >= 5:
                    backlinks.append({
                        "file_path": str(row[0]),
                        "doc_title": str(row[1]),
                        "label": str(row[2]),
                        "line": int(row[3]) if str(row[3]).isdigit() else 1,
                        "link_type": str(row[4]) if row[4] else "wiki_link",
                    })

        return {
            "symbol": clean_name,
            "definitions": definitions,
            "imports": imports,
            "references": references,
            "backlinks": backlinks,
        }

    def get_impact_tree(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Returns all downstream files that import or reference any symbol defined in file_path.
        (Impact analysis for refactoring).
        """
        file_path = str(file_path).replace("\\", "/")
        res = self._query(
            "MATCH (f_src:File {path: $path})<-[:DEFINED_IN]-(s:Symbol)<-[r]-(f_dep:File) "
            "WHERE f_dep.path <> $path "
            "RETURN DISTINCT f_dep.path, s.name, type(r), r.line",
            {"path": file_path},
        )
        dependents = []
        if res and len(res) > 1 and isinstance(res[1], list):
            for row in res[1]:
                if len(row) >= 4:
                    dependents.append({
                        "file_path": str(row[0]),
                        "symbol": str(row[1]),
                        "relation": str(row[2]),
                        "line": int(row[3]) if str(row[3]).isdigit() else 1,
                    })
        return dependents

    def get_statistics(self) -> Dict[str, int]:
        """Returns counts of symbols, files, documents, tags, links, declarations, imports, and references."""
        counts = {
            "symbols": 0,
            "files": 0,
            "documents": 0,
            "tags": 0,
            "links": 0,
            "definitions": 0,
            "imports": 0,
            "references": 0,
        }

        sym_res = self._query("MATCH (s:Symbol) RETURN count(s)")
        if sym_res and len(sym_res) > 1 and sym_res[1]:
            counts["symbols"] = int(sym_res[1][0][0])

        file_res = self._query("MATCH (f:File) RETURN count(f)")
        if file_res and len(file_res) > 1 and file_res[1]:
            counts["files"] = int(file_res[1][0][0])

        doc_res = self._query("MATCH (d:Document) RETURN count(d)")
        if doc_res and len(doc_res) > 1 and doc_res[1]:
            counts["documents"] = int(doc_res[1][0][0])

        tag_res = self._query("MATCH (t:Tag) RETURN count(t)")
        if tag_res and len(tag_res) > 1 and tag_res[1]:
            counts["tags"] = int(tag_res[1][0][0])

        link_res = self._query("MATCH ()-[r:LINKS_TO]->() RETURN count(r)")
        if link_res and len(link_res) > 1 and link_res[1]:
            counts["links"] = int(link_res[1][0][0])

        def_res = self._query("MATCH ()-[r:DEFINED_IN]->() RETURN count(r)")
        if def_res and len(def_res) > 1 and def_res[1]:
            counts["definitions"] = int(def_res[1][0][0])

        imp_res = self._query("MATCH ()-[r:IMPORTS]->() RETURN count(r)")
        if imp_res and len(imp_res) > 1 and imp_res[1]:
            counts["imports"] = int(imp_res[1][0][0])

        ref_res = self._query("MATCH ()-[r:REFERENCES]->() RETURN count(r)")
        if ref_res and len(ref_res) > 1 and ref_res[1]:
            counts["references"] = int(ref_res[1][0][0])

        return counts
