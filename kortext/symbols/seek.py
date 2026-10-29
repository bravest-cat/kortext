"""
Kortext Anti-Ripgrep Deterministic Symbol Seek & Smart Code Search Engine.
Eliminates repeated ripgrep loops by providing B-Tree symbol seeks and knapsack-bounded regex.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kortext.falkordb_client import get_shared_falkordb_client
from kortext.symbols.formatter import format_impact_report

logger = logging.getLogger("kortext.symbols.seek")

# Unified Stop Words: English query terms, auxiliaries, prepositions, programming keywords, file extensions
STOP_WORDS = {
    # English question words & pronouns
    "where", "what", "when", "why", "how", "who", "which", "whose", "whom",
    "this", "that", "these", "those", "it", "its", "you", "your", "we", "our", "they", "their",
    "i", "my", "me", "he", "she", "him", "her", "them",
    # Auxiliary & common conversational verbs
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "done", "doing",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "work", "works", "working", "worked",
    "run", "runs", "running", "ran",
    "use", "uses", "using", "used",
    "make", "makes", "making", "made",
    "get", "gets", "getting", "got",
    "set", "sets", "setting",
    "help", "helps", "please",
    "know", "knows", "see",
    # Prepositions, conjunctions & determiners
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "up", "about",
    "into", "over", "after", "before", "between", "under", "through", "during", "without",
    "and", "or", "not", "but", "if", "then", "else", "so", "than", "too",
    "the", "a", "an", "all", "any", "some", "each", "every", "both", "either", "neither",
    "only", "also", "just", "more", "most", "other", "such", "very",
    # Query intent terms
    "query", "test", "tests", "code", "file", "files", "line", "lines",
    "find", "show", "look", "search", "list", "give", "tell", "explain", "describe",
    "defined", "definition", "references", "callers", "callees", "calls", "called", "calling",
    "implement", "implements", "implementation",
    "match", "matches", "matching", "normal", "english", "text", "texts", "word", "words", "capitalized",
    # Programming keywords & literals
    "class", "def", "function", "method", "variable",
    "self", "cls", "true", "false", "none", "null", "nil",
    "return", "returns", "import", "export", "async", "await", "yield",
    "try", "except", "catch", "raise", "throw", "finally",
    "while", "break", "continue", "pass", "const", "let", "var",
    "public", "private", "protected", "static",
    # Common file extensions
    "py", "md", "txt", "json", "toml", "yaml", "yml", "rst", "ts", "js", "html", "css",
}


def extract_symbol_candidates(
    query: str,
    max_candidates: int = 5,
    qdrant_engine: Optional[Any] = None,
) -> List[str]:
    """
    Extracts likely symbol identifiers from a natural language query.
    Canonical implementation supporting PascalCase, camelCase, snake_case,
    dotted identifiers, backtick quotes, call syntax, and intent prefixes.
    Includes a keyword fallback for prose questions (Weakness 2.4) generating
    snake_case, PascalCase, and camelCase variations of adjacent keywords.
    Filters out common English stop words and programming keywords.
    """
    candidates: List[str] = []

    # 1. Backtick quotes: e.g. `SymbolName`, `kortext.symbols.seek`
    candidates.extend(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", query))

    # 2. Intent prefixes: e.g. 'where is X', 'def X', 'find X', 'references to X'
    intent_matches = re.findall(
        r"(?:where is|what is|find|show|look up|inspect|references to|callers of|calls to|definition of|declaration of|class|def|function|method|type|interface|struct)\s+(?:class\s+|def\s+|function\s+|method\s+|symbol\s+|type\s+|interface\s+|struct\s+)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)*)",
        query,
        flags=re.IGNORECASE,
    )
    candidates.extend(intent_matches)

    # 3. Call syntax: foo(), obj.method()
    candidates.extend(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)*)\s*\(", query))

    # 4. Dotted identifiers: e.g. kortext.symbols.seek, SymbolSeekEngine.seek_symbol
    dotted_matches = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)+\b", query)
    for dm in dotted_matches:
        candidates.append(dm)
        # Also extract leaf component if it's a valid symbol
        leaf = dm.split(".")[-1]
        if len(leaf) >= 3 and leaf.lower() not in STOP_WORDS:
            candidates.append(leaf)

    # 5. PascalCase identifiers: e.g. SymbolGraphManager, ASTCodeSplitter, QdrantClient
    candidates.extend(re.findall(r"\b(?:[A-Z]+[a-z0-9]+[A-Z][a-zA-Z0-9]*|[A-Z]{2,}[a-z0-9]+[a-zA-Z0-9]*)\b", query))

    # 6. camelCase identifiers: e.g. extractSymbolCandidates, getElementById
    candidates.extend(re.findall(r"\b[a-z][a-z0-9]*[A-Z][a-zA-Z0-9]*\b", query))

    # 7. snake_case / SCREAMING_SNAKE_CASE: e.g. extract_symbol_candidates, _extract_symbol_candidates, MAX_RETRIES
    candidates.extend(re.findall(r"\b_?[a-zA-Z0-9]+(?:_[a-zA-Z0-9]+)+\b", query))

    # 8. Single identifier query: e.g. exact symbol name as prompt
    clean_q = query.strip()
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", clean_q):
        candidates.append(clean_q)

    seen = set()
    result = []
    for c in candidates:
        c = c.strip("`()")
        low = c.lower()
        if low not in STOP_WORDS and c not in seen and len(c) >= 3:
            seen.add(c)
            result.append(c)
            if len(result) >= max_candidates:
                break

    # 9. Semantic & Keyword Fallback (Weakness 2.4): Natural language prose questions
    # When queries like "how does token optimizer work?" yield no regex candidates,
    # or only simple single-word intent matches (e.g. "where is retrieval engine defined?").
    has_structured_symbol = any(re.search(r"[A-Z_.]", c[1:]) for c in result)
    if not result or not has_structured_symbol:
        raw_tokens = re.findall(r"[A-Za-z0-9]+", query)

        # Group contiguous non-stopword tokens into adjacent keyword chunks
        chunks: List[List[str]] = []
        current_chunk: List[str] = []
        for t in raw_tokens:
            low = t.lower()
            if low not in STOP_WORDS and len(t) >= 2:
                current_chunk.append(t)
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = []
        if current_chunk:
            chunks.append(current_chunk)

        fallback_candidates: List[str] = []
        for chunk in chunks:
            # Multi-word combinations (adjacent keywords)
            if len(chunk) >= 2:
                # 1. snake_case: "token_optimizer"
                fallback_candidates.append("_".join(w.lower() for w in chunk))
                # 2. PascalCase: "TokenOptimizer"
                fallback_candidates.append("".join(w.capitalize() for w in chunk))
                # 3. camelCase: "tokenOptimizer"
                camel = chunk[0].lower() + "".join(w.capitalize() for w in chunk[1:])
                fallback_candidates.append(camel)
                # 4. If chunk has > 2 words (e.g. ["symbol", "seek", "engine"]), generate bigrams
                if len(chunk) > 2:
                    for i in range(len(chunk) - 1):
                        pair = [chunk[i], chunk[i + 1]]
                        fallback_candidates.append(f"{pair[0].lower()}_{pair[1].lower()}")
                        fallback_candidates.append(f"{pair[0].capitalize()}{pair[1].capitalize()}")
                        fallback_candidates.append(f"{pair[0].lower()}{pair[1].capitalize()}")

            # Individual words in chunk (>= 3 chars)
            for w in chunk:
                if len(w) >= 3 and w.lower() not in STOP_WORDS:
                    fallback_candidates.append(w.lower())
                    if not w.islower() and not w.isupper():
                        fallback_candidates.append(w)
                    fallback_candidates.append(w.capitalize())

        # If fallback produced candidates, prioritize multi-word variations ahead of single-word intent matches
        combined = fallback_candidates + result
        merged_seen = set()
        merged_result = []
        for c in combined:
            if c not in merged_seen and len(c) >= 3 and c.lower() not in STOP_WORDS:
                merged_seen.add(c)
                merged_result.append(c)
                if len(merged_result) >= max_candidates:
                    break
        result = merged_result

    # 10. Dense Vector Retrieval Fallback:
    # When regex and keyword tokenization return empty [], query codebase collection if qdrant_engine is provided.
    # Respects Sole Custodian invariant: never instantiates EmbeddedQdrantEngine directly.
    if not result and qdrant_engine is not None:
        try:
            client = getattr(qdrant_engine, "client", qdrant_engine)
            target_collection = os.getenv("KORTEXT_COLLECTION", os.getenv("QDRANT_COLLECTION", "kortext_codebase"))

            # Check collection existence if available; fall back to "codebase" alias if needed
            check_fn = getattr(client, "collection_exists", None) or getattr(qdrant_engine, "collection_exists", None)
            if callable(check_fn):
                try:
                    if not check_fn(target_collection):
                        if check_fn("codebase"):
                            target_collection = "codebase"
                except Exception:
                    logger.debug("Suppressed non-fatal exception")

            from kortext.embeddings import get_dense_embedder

            q_vec = get_dense_embedder().embed_query(query)
            resp = None
            query_fn = getattr(client, "query_points", None) or getattr(qdrant_engine, "query_points", None)
            if callable(query_fn):
                try:
                    resp = query_fn(collection_name=target_collection, query=q_vec, using="text-dense", limit=2)
                except Exception as q_err:
                    if target_collection == "kortext_codebase":
                        try:
                            resp = query_fn(collection_name="codebase", query=q_vec, using="text-dense", limit=2)
                        except Exception:
                            logger.debug(f"Dense vector fallback query failed: {q_err}")
                    else:
                        logger.debug(f"Dense vector fallback query failed: {q_err}")

            if resp is not None:
                points = getattr(resp, "points", resp)
                if not isinstance(points, list) and hasattr(points, "__iter__"):
                    points = list(points)
                for pt in points:
                    payload = getattr(pt, "payload", {}) or {}
                    if not isinstance(payload, dict):
                        payload = {}
                    metadata = payload.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = {}

                    extracted_names: List[str] = []
                    for source in (payload, metadata):
                        for key in ("defined_symbols", "symbols", "name"):
                            val = source.get(key)
                            if isinstance(val, list):
                                extracted_names.extend([str(v) for v in val if v])
                            elif isinstance(val, str) and val:
                                extracted_names.append(val)

                    for name in extracted_names:
                        clean_name = name.strip("`()")
                        if clean_name and clean_name not in seen and clean_name.lower() not in STOP_WORDS:
                            seen.add(clean_name)
                            result.append(clean_name)
                            if len(result) >= max_candidates:
                                break
                    if len(result) >= max_candidates:
                        break
        except Exception as e:
            logger.debug(f"Dense vector fallback query failed: {e}")

    return result


# Backwards compatibility alias
_extract_symbol_candidates = extract_symbol_candidates


def extract_symbol_source_detail(
    file_path: Optional[str],
    line: Optional[int],
    end_line: Optional[int],
    kind: str,
    signature: str,
    docstring: Optional[str],
    callers: List[Any],
    callees: List[Any],
    detail_level: str = "skeleton",
) -> Dict[str, Any]:
    """
    Extracts and trims code representation according to detail_level:
    - 'skeleton': definition signature, docstring, type annotations, and inner body replaced with '...'
    - 'interface': skeleton + method signatures, callers, and callees
    - 'full': complete unabridged source code implementation
    Gracefully falls back to declaration signature + docstring when AST or file is unavailable.
    """
    level = detail_level.lower().strip() if isinstance(detail_level, str) else "skeleton"
    if level not in ("skeleton", "interface", "full"):
        level = "skeleton"

    clean_sig = (signature or "").strip()
    doc = (docstring or "").strip()
    non_py_exts = {
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
        ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash", ".zsh",
    }
    ext = Path(file_path).suffix.lower() if file_path else ""
    if ext == ".py":
        is_py = True
    elif ext in non_py_exts:
        is_py = False
    elif not file_path:
        is_py = clean_sig.startswith(("def ", "async def ")) or clean_sig.rstrip().endswith(":")
    else:
        is_py = clean_sig.startswith(("def ", "async def ")) or clean_sig.rstrip().endswith(":")

    if is_py:
        sig_header = clean_sig.rstrip(":")
        if doc:
            fallback_decl = f"{sig_header}:\n    \"\"\"{doc}\"\"\""
            fallback_skel = f"{sig_header}:\n    \"\"\"{doc}\"\"\"\n    ..."
        else:
            fallback_decl = sig_header
            fallback_skel = f"{sig_header}:\n    ..."
    else:
        sig_header = clean_sig.rstrip("{").strip()
        if doc:
            fallback_decl = f"/* {doc} */\n{sig_header}"
            fallback_skel = f"/* {doc} */\n{sig_header} {{\n    ...\n}}"
        else:
            fallback_decl = sig_header
            fallback_skel = f"{sig_header} {{\n    ...\n}}"

    full_code: Optional[str] = None
    skeleton_code: Optional[str] = None
    interface_code: Optional[str] = None
    methods: List[str] = []

    # Attempt to locate source file on disk
    target_p: Optional[Path] = None
    if file_path:
        p = Path(file_path)
        if p.is_file():
            target_p = p
        else:
            cand1 = (Path.cwd() / p).resolve()
            if cand1.is_file():
                target_p = cand1

    if target_p and target_p.is_file():
        try:
            source_bytes = target_p.read_bytes()
            source_text = source_bytes.decode("utf-8", errors="replace")
            file_lines = source_text.splitlines()

            if line and end_line and line > 0 and end_line >= line:
                s_idx = max(0, line - 1)
                e_idx = min(len(file_lines), end_line)
                raw_slice = "\n".join(file_lines[s_idx:e_idx])
                full_code = raw_slice

                if is_py:
                    try:
                        dedented = textwrap.dedent(raw_slice)
                        parsed = ast.parse(dedented)
                        if parsed.body:
                            top_node = parsed.body[0]
                            if isinstance(top_node, ast.ClassDef):
                                c_doc = ast.get_docstring(top_node) or doc
                                # 1. Skeleton for ClassDef
                                skel_body: list[ast.stmt] = []
                                if c_doc:
                                    skel_body.append(ast.Expr(value=ast.Constant(value=c_doc)))
                                for stmt in top_node.body:
                                    if isinstance(stmt, ast.AnnAssign):
                                        skel_body.append(stmt)
                                skel_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))

                                skel_ast = ast.ClassDef(
                                    name=top_node.name,
                                    bases=top_node.bases,
                                    keywords=top_node.keywords,
                                    body=skel_body,
                                    decorator_list=top_node.decorator_list,
                                    lineno=top_node.lineno,
                                    col_offset=top_node.col_offset,
                                )
                                ast.fix_missing_locations(skel_ast)
                                skeleton_code = ast.unparse(skel_ast)

                                # 2. Interface for ClassDef
                                if_body: list[ast.stmt] = []
                                if c_doc:
                                    if_body.append(ast.Expr(value=ast.Constant(value=c_doc)))
                                for stmt in top_node.body:
                                    if isinstance(stmt, ast.AnnAssign):
                                        if_body.append(stmt)
                                for stmt in top_node.body:
                                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                        m_doc = ast.get_docstring(stmt)
                                        m_body: list[ast.stmt] = [ast.Expr(value=ast.Constant(value=Ellipsis))]
                                        if m_doc:
                                            m_body.insert(0, ast.Expr(value=ast.Constant(value=m_doc)))
                                        m_stub = type(stmt)(
                                            name=stmt.name,
                                            args=stmt.args,
                                            body=m_body,
                                            decorator_list=stmt.decorator_list,
                                            returns=stmt.returns,
                                            type_comment=getattr(stmt, "type_comment", None),
                                            lineno=stmt.lineno,
                                            col_offset=stmt.col_offset,
                                        )
                                        if_body.append(m_stub)
                                        unparsed_lines = ast.unparse(m_stub).splitlines()
                                        sig_lines = []
                                        for ul in unparsed_lines:
                                            if ul.strip().startswith(('"""', "'''", "...")):
                                                break
                                            sig_lines.append(ul)
                                        m_sig_str = "\n".join(sig_lines).rstrip(":")
                                        methods.append(m_sig_str)

                                if_ast = ast.ClassDef(
                                    name=top_node.name,
                                    bases=top_node.bases,
                                    keywords=top_node.keywords,
                                    body=if_body,
                                    decorator_list=top_node.decorator_list,
                                    lineno=top_node.lineno,
                                    col_offset=top_node.col_offset,
                                )
                                ast.fix_missing_locations(if_ast)
                                interface_code = ast.unparse(if_ast)

                            elif isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                fn_doc = ast.get_docstring(top_node) or doc
                                fn_body: list[ast.stmt] = [ast.Expr(value=ast.Constant(value=Ellipsis))]
                                if fn_doc:
                                    fn_body.insert(0, ast.Expr(value=ast.Constant(value=fn_doc)))

                                fn_skel = type(top_node)(
                                    name=top_node.name,
                                    args=top_node.args,
                                    body=fn_body,
                                    decorator_list=top_node.decorator_list,
                                    returns=top_node.returns,
                                    type_comment=getattr(top_node, "type_comment", None),
                                    lineno=top_node.lineno,
                                    col_offset=top_node.col_offset,
                                )
                                ast.fix_missing_locations(fn_skel)
                                skeleton_code = ast.unparse(fn_skel)
                                interface_code = skeleton_code
                    except Exception:
                        logger.debug("Suppressed non-fatal exception")
        except Exception:
            logger.debug("Suppressed non-fatal exception")

    # Fallbacks if AST processing did not yield outputs
    if not full_code:
        full_code = fallback_decl
    if not skeleton_code:
        skeleton_code = fallback_skel
    if not interface_code:
        interface_code = skeleton_code

    if level == "full":
        code_out = full_code
    elif level == "interface":
        code_out = interface_code
    else:
        code_out = skeleton_code

    return {
        "code": code_out,
        "declaration": fallback_decl,
        "skeleton_code": skeleton_code,
        "methods": methods,
        "detail_level": level,
    }


class SymbolSeekEngine:
    """Deterministic symbol locator and blast-radius traverser."""

    extract_symbol_candidates = staticmethod(extract_symbol_candidates)

    def __init__(
        self,
        qdrant_engine: Optional[Any] = None,
        graph_manager: Optional[Any] = None,
    ):
        self.client = get_shared_falkordb_client()
        self.qdrant_engine = qdrant_engine
        self.graph_manager = graph_manager
        self._cache: Dict[Tuple[str, float], str] = {}
        self._symbol_to_file: Dict[str, str] = {}
        self._cache_maxsize: int = 1024

    def clear_cache(self) -> None:
        """Clears the in-memory symbol lookup cache."""
        self._cache.clear()
        self._symbol_to_file.clear()

    def seek_symbol(
        self,
        symbol_name: str,
        detail_level: str = "skeleton",
        file_hint: Optional[str] = None,
        graph_name: Optional[str] = None,
        workspace_root: Optional[str] = None,
    ) -> str:
        """
        Locates exact symbol declaration, signature, docstring, callers, and callees.
        Returns dense structured JSON consuming <150 tokens.
        """
        level = detail_level.lower().strip() if isinstance(detail_level, str) else "skeleton"
        if level not in ("skeleton", "interface", "full"):
            if file_hint is None and ("." in str(detail_level) or "/" in str(detail_level) or "\\" in str(detail_level)):
                file_hint = detail_level
                level = "skeleton"
            else:
                level = "skeleton"

        default_sym_graph = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
        target_graph = graph_name or default_sym_graph

        def _make_cache_key(mtime: float) -> Any:
            if level == "skeleton" and not graph_name and not workspace_root and not file_hint:
                return (symbol_name, mtime)
            elif not graph_name and not workspace_root and not file_hint:
                return (f"{symbol_name}:{level}", mtime)
            return (f"{symbol_name}:{level}:{target_graph}:{workspace_root or ''}:{file_hint or ''}", mtime)

        # Fast Path LRU Cache check: (symbol_name, file_mtime)
        file_path = file_hint or self._symbol_to_file.get(symbol_name)
        if file_path:
            p = Path(file_path)
            if p.exists():
                try:
                    mtime = p.stat().st_mtime
                    cache_key = _make_cache_key(mtime)
                    if cache_key in self._cache:
                        val = self._cache.pop(cache_key)
                        self._cache[cache_key] = val
                        return val
                except OSError:
                    logger.debug("Suppressed non-fatal exception")

        # 1. Fast Path: Exact B-Tree indexed lookup on Symbol.name (Linear aggregation).
        # Null caller/callee maps are filtered out: FalkorDB panics (and the server exits) when a
        # symbol with no definition or edges, such as an unresolved-reference stub, reaches this
        # aggregation carrying `[{caller: NULL}]` beside the other null columns.
        fast_cypher = """
        MATCH (s:Symbol {name: $name})
        OPTIONAL MATCH (s)-[d:DEFINED_IN]->(f:File)
        WITH s, d, f
        OPTIONAL MATCH (caller:Symbol)-[c:CALLS]->(s)
        WITH s, d, f, [x IN collect(DISTINCT {caller: caller.name, file: caller.file_path, line: c.line}) WHERE x.caller IS NOT NULL][..6] AS callers
        OPTIONAL MATCH (s)-[out:CALLS]->(callee:Symbol)
        RETURN 
            s.name AS symbol,
            s.kind AS kind,
            f.path AS file,
            d.line AS line,
            d.end_line AS end_line,
            d.signature AS signature,
            d.docstring AS docstring,
            callers,
            [x IN collect(DISTINCT {callee: callee.name, file: callee.file_path}) WHERE x.callee IS NOT NULL][..6] AS callees
        LIMIT 5
        """
        params = {"name": symbol_name}
        raw = self.client.query(target_graph, fast_cypher, params, read_only=True)

        # 2. Fallback Path: Substring search on qualified_name if exact match missed
        if not raw or len(raw) < 2 or not raw[1]:
            fallback_cypher = """
            MATCH (s:Symbol)
            WHERE s.qualified_name CONTAINS $name
            WITH s LIMIT 25
            OPTIONAL MATCH (s)-[d:DEFINED_IN]->(f:File)
            WITH s, d, f
            OPTIONAL MATCH (caller:Symbol)-[c:CALLS]->(s)
            WITH s, d, f, [x IN collect(DISTINCT {caller: caller.name, file: caller.file_path, line: c.line}) WHERE x.caller IS NOT NULL][..6] AS callers
            OPTIONAL MATCH (s)-[out:CALLS]->(callee:Symbol)
            RETURN 
                s.name AS symbol,
                s.kind AS kind,
                f.path AS file,
                d.line AS line,
                d.end_line AS end_line,
                d.signature AS signature,
                d.docstring AS docstring,
                callers,
                [x IN collect(DISTINCT {callee: callee.name, file: callee.file_path}) WHERE x.callee IS NOT NULL][..6] AS callees
            LIMIT 5
            """
            raw = self.client.query(target_graph, fallback_cypher, params, read_only=True)

        # 3. Natural Language Prose & Semantic Fallback: Extract candidates and search against symbol names
        if not raw or len(raw) < 2 or not raw[1]:
            candidates = extract_symbol_candidates(symbol_name, qdrant_engine=getattr(self, "qdrant_engine", None))
            for cand in candidates:
                if cand == symbol_name:
                    continue
                cand_params = {"name": cand}
                cand_raw = self.client.query(target_graph, fast_cypher, cand_params, read_only=True)
                if cand_raw and len(cand_raw) >= 2 and cand_raw[1]:
                    raw = cand_raw
                    break
                cand_raw = self.client.query(target_graph, fallback_cypher, cand_params, read_only=True)
                if cand_raw and len(cand_raw) >= 2 and cand_raw[1]:
                    raw = cand_raw
                    break

        if not raw or len(raw) < 2 or not raw[1]:
            return json.dumps({"status": "not_found", "query": symbol_name, "detail_level": level})

        matches = []
        primary_file: Optional[str] = None
        for row in raw[1]:
            f_path = row[2]
            if not primary_file and f_path:
                primary_file = f_path
            code_meta = extract_symbol_source_detail(
                file_path=f_path,
                line=row[3],
                end_line=row[4],
                kind=row[1],
                signature=row[5],
                docstring=row[6],
                callers=row[7] if isinstance(row[7], list) else [],
                callees=row[8] if isinstance(row[8], list) else [],
                detail_level=level,
            )
            match_entry = {
                "symbol": row[0],
                "kind": row[1],
                "file": f_path,
                "lines": [row[3], row[4]],
                "signature": row[5],
                "docstring": (row[6] or "")[:120].strip(),
                "callers": row[7],
                "callees": row[8],
                "detail_level": level,
                "code": code_meta["code"],
            }
            if level == "interface":
                match_entry["methods"] = code_meta["methods"]
            elif level == "full":
                match_entry["declaration"] = code_meta["declaration"]
            matches.append(match_entry)

        # Deduplicate matches pointing to the same resolved realpath / symbol
        seen_keys = set()
        unique_matches = []
        for m in matches:
            f = m.get("file")
            # Exclude non-code markdown files and section headings
            if f and str(f).lower().endswith((".md", ".markdown")):
                continue
            k = m.get("kind")
            if k and (str(k).lower().endswith("_section") or str(k).lower() == "section"):
                continue

            try:
                real_f = str(Path(f).resolve()) if f else ""
            except Exception:
                real_f = str(f)
            key = (m.get("symbol"), real_f)
            if key not in seen_keys:
                seen_keys.add(key)
                unique_matches.append(m)
        matches = unique_matches

        # Filter or prioritize matches if file_hint is provided
        if file_hint:
            hint_matches = [
                m for m in matches
                if m.get("file") and (file_hint in str(m["file"]) or str(m["file"]).endswith(file_hint))
            ]
            if hint_matches:
                matches = hint_matches

        # Filter or prioritize matches if workspace_root is provided
        if workspace_root:
            in_ws = [
                m for m in matches
                if m.get("file") and (
                    str(m["file"]).startswith(workspace_root)
                    or (not Path(str(m["file"])).is_absolute() and (Path(workspace_root) / str(m["file"])).exists())
                )
            ]
            if in_ws:
                matches = in_ws
            else:
                matches.sort(
                    key=lambda m: 0 if (m.get("file") and str(m["file"]).startswith(workspace_root)) else 1
                )

        # Attach active AST-grounded invariants (from journal/grounded_invariants.json) to matched symbols
        grounded_for_matches: List[Dict[str, Any]] = []
        try:
            from kortext.invariants import grounded_read_path

            g_file = grounded_read_path()

            if g_file.is_file():
                g_items = json.loads(g_file.read_text(encoding="utf-8"))
                if isinstance(g_items, list):
                    for m in matches:
                        m_sym = str(m.get("symbol") or "")
                        m_file = str(m.get("file") or "")
                        m_invs: List[Dict[str, str]] = []
                        for g_inv in g_items:
                            if not isinstance(g_inv, dict) or g_inv.get("quarantined"):
                                continue
                            inv_sym = str(g_inv.get("symbol_name") or "")
                            inv_fp = str(g_inv.get("file_path") or "")
                            if (m_sym and inv_sym == m_sym) or (
                                m_file and inv_fp and Path(m_file).name == Path(inv_fp).name
                            ):
                                entry_inv = {
                                    "domain": str(g_inv.get("domain") or "INV"),
                                    "symbol": inv_sym,
                                    "rule": str(g_inv.get("rule") or ""),
                                }
                                m_invs.append(entry_inv)
                                if entry_inv not in grounded_for_matches:
                                    grounded_for_matches.append(entry_inv)
                        if m_invs:
                            m["grounded_invariants"] = m_invs
        except Exception as inv_exc:
            logger.debug("Grounded invariants lookup in seek_symbol skipped: %s", inv_exc)

        payload_out: Dict[str, Any] = {"status": "found", "matches": matches, "detail_level": level}
        if grounded_for_matches:
            payload_out["grounded_invariants"] = grounded_for_matches
        result_json = json.dumps(payload_out, separators=(",", ":"))

        # Populate LRU cache keyed on (symbol_name, level, target_graph, workspace_root, file_mtime)
        target_file = file_hint or primary_file
        if target_file:
            p = Path(target_file)
            if p.exists():
                try:
                    mtime = p.stat().st_mtime
                    cache_key = _make_cache_key(mtime)
                    if len(self._cache) >= self._cache_maxsize:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[cache_key] = result_json
                    self._symbol_to_file[symbol_name] = str(p)
                except OSError as os_exc:
                    logger.debug("LRU cache stat skipped in seek_symbol: %s", os_exc)

        return result_json

    def trace_impact(
        self,
        symbol_name: str,
        depth: int = 2,
        direction: str = "upstream",
        token_budget: Optional[int] = 1500,
    ) -> str:
        """
        Traverses incoming callers, references, imports, and note links to compute refactoring blast radius.
        Delegates to SymbolGraphManager with Knapsack dynamic token budgeting and progressive level-of-detail.
        """
        from kortext.symbols.graph import SymbolGraphManager

        mgr = getattr(self, "graph_manager", None)
        if mgr is None:
            mgr = SymbolGraphManager(client=self.client)

        impact = mgr.trace_symbol_impact(
            symbol_name=symbol_name,
            direction=direction,
            max_depth=depth,
            token_budget=token_budget,
        )
        return json.dumps(impact, separators=(",", ":"))

    def _resolve_graph_for_workspace(self, workspace_root: Optional[str] = None) -> str:
        """
        Resolves the FalkorDB symbols graph containing files under workspace_root
        using strictly parameterized Cypher ($ws) to prevent Cypher string injection.
        """
        default_sym_graph = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
        if not workspace_root:
            return default_sym_graph
        try:
            ws_resolved = str(Path(workspace_root).resolve())
            res = self.client.query(
                default_sym_graph,
                "MATCH (f:File) WHERE f.path STARTS WITH $ws RETURN count(f) LIMIT 1",
                {"ws": ws_resolved},
                read_only=True,
            )
            if res and len(res) > 1 and res[1] and res[1][0] and int(res[1][0][0] or 0) > 0:
                return default_sym_graph
        except Exception as exc:
            logger.debug("Workspace symbol graph resolution fallback: %s", exc)
        return default_sym_graph


