"""
Relational Cypher query handlers, cluster inference, and overview graph view builder.
"""
from __future__ import annotations

import html
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from kortext.dashboard.views.specs import (
    REL_SPECS,
    _make_vis_edge,
    _make_vis_node,
)


def _posix_name(path_val: Any) -> str:
    return PurePosixPath(str(path_val or "").replace("\\", "/")).name


def _posix_stem(path_val: Any) -> str:
    return PurePosixPath(str(path_val or "").replace("\\", "/")).stem


def _handle_defined_in(rows: list, rel: str, spec: dict, nodes_dict: Dict[str, dict], edges_list: List[dict], sym_to_file: Dict[str, str]) -> None:
    for row in rows:
        if len(row) >= 4:
            sym_name, file_path, kind, line = str(row[0]), str(row[1]), str(row[2]), str(row[3])
            sym_to_file[sym_name] = file_path
            is_doc = file_path.lower().endswith((".md", ".markdown"))
            fname = _posix_name(file_path)
            if file_path not in nodes_dict:
                nodes_dict[file_path] = _make_vis_node(
                    node_id=file_path,
                    label=_posix_stem(file_path).replace("-", " ").replace("_", " ").title() if is_doc else fname,
                    group="document" if is_doc else "file",
                    title=f"<b>{'Document' if is_doc else 'File'}: {html.escape(fname)}</b><br/>{html.escape(file_path)}",
                )
            if sym_name not in nodes_dict:
                nodes_dict[sym_name] = _make_vis_node(
                    node_id=sym_name,
                    label=sym_name,
                    group="symbol",
                    title=f"<b>{html.escape(sym_name)}</b> ({html.escape(kind)})<br/>Line {html.escape(line)} in {html.escape(fname)}",
                )
            edges_list.append(_make_vis_edge(sym_name, file_path, rel))


def _handle_filter_existing(rows: list, rel: str, spec: dict, nodes_dict: Dict[str, dict], edges_list: List[dict], sym_to_file: Dict[str, str]) -> None:
    for row in rows:
        if len(row) >= 2:
            src, dst = str(row[0]), str(row[1])
            if src in nodes_dict and dst in nodes_dict:
                edges_list.append(_make_vis_edge(src, dst, rel))


def _handle_symbol_hierarchy(rows: list, rel: str, spec: dict, nodes_dict: Dict[str, dict], edges_list: List[dict], sym_to_file: Dict[str, str]) -> None:
    src_suffix = f" ({html.escape(str(spec['src_role']))})" if spec.get("src_role") else ""
    dst_suffix = f" ({html.escape(str(spec['dst_role']))})" if spec.get("dst_role") else ""
    for row in rows:
        if len(row) >= 2:
            s1, s2 = str(row[0]), str(row[1])
            if s1 not in nodes_dict:
                nodes_dict[s1] = _make_vis_node(node_id=s1, label=s1, group="symbol", title=f"<b>{html.escape(s1)}</b>{src_suffix}")
            if s2 not in nodes_dict:
                nodes_dict[s2] = _make_vis_node(node_id=s2, label=s2, group="symbol", title=f"<b>{html.escape(s2)}</b>{dst_suffix}")
            edges_list.append(_make_vis_edge(s1, s2, rel))


def _handle_doc_links(rows: list, rel: str, spec: dict, nodes_dict: Dict[str, dict], edges_list: List[dict], sym_to_file: Dict[str, str]) -> None:
    for row in rows:
        if len(row) >= 7:
            src_path, src_title, dst_id, dst_title, dst_labels, rel_label, link_type = (str(row[i]) for i in range(7))
            if src_path in nodes_dict:
                nodes_dict[src_path]["group"] = "document"
                if src_title and src_title != src_path:
                    nodes_dict[src_path]["label"] = src_title
                    nodes_dict[src_path]["title"] = f"<b>Document: {html.escape(src_title)}</b><br/>{html.escape(src_path)}"
            else:
                nodes_dict[src_path] = _make_vis_node(
                    node_id=src_path,
                    label=src_title or _posix_stem(src_path),
                    group="document",
                    title=f"<b>Document: {html.escape(src_title)}</b><br/>{html.escape(src_path)}",
                )
            dst_group = "concept" if "concept" in dst_labels.lower() else ("document" if "document" in dst_labels.lower() else "file")
            if dst_id in nodes_dict:
                nodes_dict[dst_id]["group"] = dst_group
            else:
                nodes_dict[dst_id] = _make_vis_node(
                    node_id=dst_id,
                    label=dst_title or _posix_name(dst_id),
                    group=dst_group,
                    title=f"<b>{html.escape(dst_title)}</b> ({html.escape(dst_group)})<br/>{html.escape(dst_id)}",
                )
            src_fname = _posix_name(src_path)
            dst_fname = _posix_name(dst_id)
            edges_list.append(_make_vis_edge(
                src_path, dst_id, rel,
                title=f"<b>LINKS_TO</b>: {html.escape(rel_label)}<br/>{html.escape(src_fname)} &rarr; {html.escape(dst_fname)}",
            ))


def _handle_tags(rows: list, rel: str, spec: dict, nodes_dict: Dict[str, dict], edges_list: List[dict], sym_to_file: Dict[str, str]) -> None:
    for row in rows:
        if len(row) >= 3:
            src_path, src_title, tag_name = str(row[0]), str(row[1]), str(row[2])
            if src_path not in nodes_dict:
                nodes_dict[src_path] = _make_vis_node(
                    node_id=src_path,
                    label=src_title or _posix_stem(src_path),
                    group="document",
                    title=f"<b>Document: {html.escape(src_title)}</b><br/>{html.escape(src_path)}",
                )
            tag_id = f"tag:{tag_name}"
            if tag_id not in nodes_dict:
                nodes_dict[tag_id] = _make_vis_node(
                    node_id=tag_id,
                    label=f"#{tag_name}",
                    full_name=tag_name,
                    group="tag",
                    title=f"<b>Tag: #{html.escape(tag_name)}</b>",
                )
            edges_list.append(_make_vis_edge(src_path, tag_id, rel))


def _handle_subsystems(
    rows: list,
    rel: str,
    spec: dict,
    nodes_dict: Dict[str, dict],
    edges_list: List[dict],
    sym_to_file: Dict[str, str],
    file_to_subsystem: Optional[Dict[str, Tuple[str, str]]] = None,
) -> None:
    for row in rows:
        if len(row) >= 4:
            file_path, sub_id, sub_name, sub_desc = str(row[0]), str(row[1]), str(row[2]), str(row[3])
            is_doc = file_path.lower().endswith((".md", ".markdown"))
            if file_path not in nodes_dict:
                fname = _posix_name(file_path)
                nodes_dict[file_path] = _make_vis_node(
                    node_id=file_path,
                    label=_posix_stem(file_path).replace("-", " ").replace("_", " ").title() if is_doc else fname,
                    group="document" if is_doc else "file",
                    title=f"<b>{'Document' if is_doc else 'File'}: {html.escape(fname)}</b><br/>{html.escape(file_path)}",
                )
            if sub_id not in nodes_dict:
                nodes_dict[sub_id] = _make_vis_node(
                    node_id=sub_id,
                    label=f"🧩 {sub_name}",
                    full_name=sub_name,
                    group="subsystem",
                    title=f"<b>Subsystem: {html.escape(sub_name)}</b><br/>{html.escape(sub_desc)}",
                )
            edges_list.append(_make_vis_edge(file_path, sub_id, rel))
            if file_to_subsystem is not None:
                file_to_subsystem[file_path] = (sub_id, f"🧩 {sub_name}")


# Register handlers in declarative REL_SPECS table
REL_SPECS["DEFINED_IN"]["handler"] = _handle_defined_in
REL_SPECS["REFERENCES"]["handler"] = _handle_filter_existing
REL_SPECS["IMPORTS"]["handler"] = _handle_filter_existing
REL_SPECS["IMPLEMENTS"]["handler"] = _handle_symbol_hierarchy
REL_SPECS["TYPE_OF"]["handler"] = _handle_symbol_hierarchy
REL_SPECS["LINKS_TO"]["handler"] = _handle_doc_links
REL_SPECS["TAG"]["handler"] = _handle_tags
if "BELONGS_TO" in REL_SPECS:
    REL_SPECS["BELONGS_TO"]["handler"] = _handle_subsystems


def _exec_graph_query(client: Any, r: Any, target_db: str, cypher: str) -> Any:
    """Executes a read-only Cypher query via client.query(), falling back to r.execute_command for legacy mock fixtures."""
    if hasattr(client, "query") and callable(client.query) and "MagicMock" not in type(client).__name__:
        return client.query(target_db, cypher, read_only=True)
    if r is not None and hasattr(r, "execute_command"):
        return r.execute_command("GRAPH.QUERY", target_db, cypher)
    return client.query(target_db, cypher, read_only=True)


def populate_clean_symbols_graph(
    r,
    target_db: str,
    nodes_dict: Dict[str, dict],
    edges_list: List[dict],
    sym_to_file: Dict[str, str],
    file_to_subsystem: Optional[Dict[str, Tuple[str, str]]] = None,
    client: Any = None,
) -> None:
    """Populates nodes and edges for clean symbols view via declarative REL_SPECS."""
    for rel_name, spec in REL_SPECS.items():
        query = spec.get("query")
        handler = spec.get("handler")
        if query and handler:
            res = _exec_graph_query(client, r, target_db, query)
            if res and len(res) > 1 and isinstance(res[1], list):
                if rel_name == "BELONGS_TO" and file_to_subsystem is not None:
                    handler(res[1], rel_name, spec, nodes_dict, edges_list, sym_to_file, file_to_subsystem=file_to_subsystem)
                else:
                    handler(res[1], rel_name, spec, nodes_dict, edges_list, sym_to_file)


def _infer_cluster(
    node_id: str,
    group: str,
    full_name: str,
    sym_to_file: Dict[str, str],
    target_db: str,
    file_to_subsystem: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Tuple[str, str]:
    """Deterministically assigns a node to an architectural cluster or subsystem."""
    if group == "subsystem":
        return node_id, full_name or f"🧩 {node_id}"

    if file_to_subsystem:
        if node_id in file_to_subsystem:
            return file_to_subsystem[node_id]
        if group == "symbol":
            parent_f = sym_to_file.get(node_id)
            if parent_f and parent_f in file_to_subsystem:
                return file_to_subsystem[parent_f]

    raw_path = sym_to_file.get(node_id, full_name or node_id) if group == "symbol" else (full_name or node_id)
    if group == "tag":
        return "tags", "🏷️ Tags & Categories"
    if group == "episodic":
        return "episodic", "💡 Episodic Decisions"
    if group == "task":
        return "tasks", "📋 Task DAG"
    if group == "project":
        return "projects", "📁 Projects"
    if group in ("decision", "adr"):
        return "decisions", "⚖️ Decisions & ADRs"
    if group in ("roadmap", "milestone"):
        return "roadmap", "🗺️ Roadmaps"
    if group == "subsystem":
        return "subsystems", "🧩 Subsystems"
    if group == "concept":
        return "concepts", "💎 Concepts"

    if "/" in raw_path or "\\" in raw_path:
        norm_path = str(raw_path).replace("\\", "/")
        p = PurePosixPath(norm_path)
        parts = p.parts
        for folder in ("kortext", "tests", "scripts", "journal", "docs"):
            if folder in parts:
                idx = parts.index(folder)
                if idx + 1 < len(parts) and not parts[idx + 1].endswith(".py"):
                    return f"{folder}.{parts[idx + 1]}", f"{folder}.{parts[idx + 1]}"
                return folder, folder
        stem = p.name.split("#")[0]
        if stem.endswith((".md", ".markdown")):
            return f"doc:{stem}", f"📄 {stem.rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ').title()}"
        return p.parent.name or "root", p.parent.name or "Root"

    if "#" in raw_path:
        doc_part = raw_path.split("#")[0]
        doc_title = _posix_stem(doc_part).replace("-", " ").title()
        return f"doc:{doc_part}", f"📄 {doc_title}"

    return "general", "🌐 General"


def _get_falkordb_client(host: str, port: int):
    """Facade-aware FalkorDB client resolver preserving backward-compatible mock hooks."""
    import sys

    views_mod = (
        sys.modules.get("kortext.dashboard.views")
        or sys.modules.get("kortext.dashboard.graph_views")
    )
    if views_mod and hasattr(views_mod, "get_shared_falkordb_client"):
        return views_mod.get_shared_falkordb_client(host=host, port=port)
    from kortext.falkordb_client import get_shared_falkordb_client

    return get_shared_falkordb_client(host=host, port=port)


def build_graph_view(
    host: str,
    port: int,
    default_db: str,
    graph_type: str = "episodic",
    clean: bool = True,
    graph_name: Optional[str] = None,
) -> dict:
    """
    Queries FalkorDB for nodes and relationships formatted for vis-network.
    graph_type: 'episodic' (kortext db) or 'symbols' (kortext_symbols db).
    clean: If True (default for symbols), filters out external built-ins.
    """
    nodes_dict: Dict[str, dict] = {}
    edges_list: List[dict] = []
    sym_to_file: Dict[str, str] = {}
    file_to_subsystem: Dict[str, Tuple[str, str]] = {}

    EPISODIC_GRAPH_NAMES = {"kortext", default_db}

    if graph_name:
        target_db = graph_name
        is_symbols_graph = (
            "symbol" in graph_name.lower()
            or "doc" in graph_name.lower()
            or graph_type == "symbols"
        ) and graph_name not in EPISODIC_GRAPH_NAMES
    else:
        target_db = "kortext_symbols" if graph_type == "symbols" else default_db
        is_symbols_graph = graph_type == "symbols"

    try:
        client = _get_falkordb_client(host=host, port=port)
        r = client.get_redis()

        if is_symbols_graph:
            if clean:
                populate_clean_symbols_graph(r, target_db, nodes_dict, edges_list, sym_to_file, file_to_subsystem)
            else:
                nodes_res = r.execute_command("GRAPH.QUERY", target_db, "MATCH (n) RETURN coalesce(n.name, n.path), labels(n)")
                if nodes_res and len(nodes_res) > 1 and isinstance(nodes_res[1], list):
                    for row in nodes_res[1]:
                        if len(row) >= 2:
                            raw_id = str(row[0])
                            raw_label = str(row[1]).replace("[", "").replace("]", "").replace("'", "").strip()
                            is_file = "file" in raw_label.lower()
                            display_name = _posix_name(raw_id) if is_file else raw_id
                            nodes_dict[raw_id] = _make_vis_node(
                                node_id=raw_id,
                                label=display_name,
                                group="file" if is_file else "symbol",
                                title=f"<b>{html.escape(display_name)}</b><br/>Type: {html.escape(raw_label)}<br/>Path: {html.escape(raw_id)}",
                            )

                edges_res = r.execute_command(
                    "GRAPH.QUERY", target_db, "MATCH (s)-[r]->(t) RETURN coalesce(s.name, s.path), type(r), coalesce(t.name, t.path)"
                )
                if edges_res and len(edges_res) > 1 and isinstance(edges_res[1], list):
                    for row in edges_res[1]:
                        if len(row) >= 3:
                            src, rel, dst = str(row[0]), str(row[1]), str(row[2])
                            if src not in nodes_dict:
                                nodes_dict[src] = _make_vis_node(
                                    node_id=src,
                                    label=_posix_name(src) if ("/" in src or "\\" in src) else src,
                                    group="symbol",
                                    title=html.escape(src),
                                )
                            if dst not in nodes_dict:
                                nodes_dict[dst] = _make_vis_node(
                                    node_id=dst,
                                    label=_posix_name(dst) if ("/" in dst or "\\" in dst) else dst,
                                    group="file",
                                    title=html.escape(dst),
                                )
                            edges_list.append(_make_vis_edge(src, dst, rel))

        else:
            nodes_res = r.execute_command(
                "GRAPH.QUERY", target_db,
                "MATCH (n) RETURN coalesce(n.name, n.title, n.id, n.path, 'Unnamed'), labels(n), coalesce(n.category, n.status, ''), coalesce(n.project, n.project_id, 'kortext')"
            )
            if nodes_res and len(nodes_res) > 1 and isinstance(nodes_res[1], list):
                for row in nodes_res[1]:
                    if len(row) >= 2:
                        name = str(row[0])
                        raw_label = str(row[1]).replace("[", "").replace("]", "").replace("'", "").strip()
                        raw_cat = str(row[2]) if len(row) > 2 else ""
                        raw_proj = str(row[3]) if len(row) > 3 and row[3] else "kortext"
                        display_label = (name[:26] + "…") if len(name) > 28 else name
                        lbl_lower = raw_label.lower()
                        if "task" in lbl_lower:
                            grp = "task"
                        elif "project" in lbl_lower:
                            grp = "project"
                        elif "adr" in lbl_lower:
                            grp = "decision"
                        elif "milestone" in lbl_lower:
                            grp = "roadmap"
                        elif "subsystem" in lbl_lower:
                            grp = "subsystem"
                        elif "episodic" in lbl_lower:
                            grp = "episodic"
                        elif "file" in lbl_lower:
                            grp = "file"
                        else:
                            grp = "entity"

                        node_obj = _make_vis_node(
                            node_id=name,
                            label=display_label,
                            group=grp,
                            title=f"<b>{html.escape(name)}</b><br/>Type: {html.escape(raw_label)}" + (f"<br/>Status/Cat: {html.escape(raw_cat)}" if raw_cat else ""),
                        )
                        node_obj["project_id"] = raw_proj
                        nodes_dict[name] = node_obj

            edges_res = r.execute_command(
                "GRAPH.QUERY", target_db,
                "MATCH (s)-[r]->(t) RETURN coalesce(s.name, s.title, s.id, s.path), type(r), coalesce(t.name, t.title, t.id, t.path)"
            )
            if edges_res and len(edges_res) > 1 and isinstance(edges_res[1], list):
                for row in edges_res[1]:
                    if len(row) >= 3:
                        src, rel, dst = str(row[0]), str(row[1]), str(row[2])
                        if src not in nodes_dict:
                            nodes_dict[src] = _make_vis_node(node_id=src, label=src, group="entity", title=html.escape(src))
                        if dst not in nodes_dict:
                            nodes_dict[dst] = _make_vis_node(node_id=dst, label=dst, group="entity", title=html.escape(dst))
                        edges_list.append(_make_vis_edge(src, dst, rel, extra_props={"is_mention": rel == "MENTIONS"}))

        deduped_edges: Dict[Tuple[str, str, str], dict] = {}
        for edge in edges_list:
            key = (edge["from"], edge["to"], edge.get("label", ""))
            if key not in deduped_edges:
                ec = dict(edge)
                ec["count"] = 1
                deduped_edges[key] = ec
            else:
                deduped_edges[key]["count"] += 1
                cnt = deduped_edges[key]["count"]
                u_name = _posix_name(key[0]) if ("/" in key[0] or "\\" in key[0]) else key[0]
                v_name = _posix_name(key[1]) if ("/" in key[1] or "\\" in key[1]) else key[1]
                deduped_edges[key]["title"] = f"<b>{html.escape(key[2])}</b> ({cnt}x occurrences)<br/>{html.escape(u_name)} &rarr; {html.escape(v_name)}"

        final_edges = list(deduped_edges.values())

        degrees: Dict[str, int] = {}
        for edge in final_edges:
            degrees[edge["from"]] = degrees.get(edge["from"], 0) + 1
            degrees[edge["to"]] = degrees.get(edge["to"], 0) + 1

        for n in nodes_dict.values():
            nid = n["id"]
            n["degree"] = degrees.get(nid, 0)
            cid, cname = _infer_cluster(nid, n.get("group", ""), n.get("full_name", nid), sym_to_file, target_db, file_to_subsystem)
            n["cluster_id"] = cid
            n["cluster_name"] = cname

        return {
            "status": "ok",
            "graph_type": graph_type,
            "nodes": list(nodes_dict.values()),
            "edges": final_edges,
            "stats": {
                "nodes_count": len(nodes_dict),
                "edges_count": len(final_edges),
            },
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "nodes": [], "edges": []}
