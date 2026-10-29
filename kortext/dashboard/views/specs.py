"""
Styling specifications, palette definitions, and node/edge formatting helpers for Vis.js rendering.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_REL_SPEC: Dict[str, Any] = {
    "width": 1.6,
    "color": "#6366f1",
    "highlight": "#818cf8",
    "hover": "#c7d2fe",
    "font_color": "#818cf8",
    "font": "#818cf8",
    "bg": "rgba(15, 23, 42, 0.94)",
    "dashes": False,
}

COLOR_TARGET = {"background": "#0a0f1d", "border": "#818cf8", "highlight": {"background": "#1e293b", "border": "#ffffff"}}
COLOR_HOP1 = {"background": "#0a0f1d", "border": "#f43f5e", "highlight": {"background": "#1e293b", "border": "#ffffff"}}
COLOR_HOP2 = {"background": "#0a0f1d", "border": "#fb923c", "highlight": {"background": "#1e293b", "border": "#ffffff"}}
COLOR_HOP3 = {"background": "#0a0f1d", "border": "#38bdf8", "highlight": {"background": "#1e293b", "border": "#ffffff"}}

SHADOW_TARGET = {"enabled": True, "color": "rgba(129, 140, 248, 0.45)", "size": 16, "x": 0, "y": 4}
SHADOW_HOP1 = {"enabled": True, "color": "rgba(244, 63, 94, 0.4)", "size": 12, "x": 0, "y": 3}
SHADOW_HOP2 = {"enabled": True, "color": "rgba(249, 115, 22, 0.35)", "size": 10, "x": 0, "y": 3}
SHADOW_HOP3 = {"enabled": True, "color": "rgba(56, 189, 248, 0.3)", "size": 8, "x": 0, "y": 2}

NODE_LEVEL_SPECS: Dict[int, Dict[str, Any]] = {
    0: {
        "color": COLOR_TARGET, "shadow": SHADOW_TARGET, "shape": "box", "shape_properties": {"borderRadius": 6},
        "margin": 10, "border_width": 2.5, "font": {"color": "#ffffff", "face": "ui-monospace, monospace", "size": 12, "bold": True},
    },
    1: {
        "color": COLOR_HOP1, "shadow": SHADOW_HOP1, "shape": "box", "shape_properties": {"borderRadius": 6},
        "margin": 8, "border_width": 2, "font": {"color": "#ffffff", "face": "ui-monospace, monospace", "size": 11, "bold": True},
    },
    2: {
        "color": COLOR_HOP2, "shadow": SHADOW_HOP2, "shape": "box", "shape_properties": {"borderRadius": 6},
        "margin": 8, "border_width": 1.8, "font": {"color": "#ffffff", "face": "ui-monospace, monospace", "size": 11, "bold": True},
    },
    3: {
        "color": COLOR_HOP3, "shadow": SHADOW_HOP3, "shape": "box", "shape_properties": {"borderRadius": 6},
        "margin": 8, "border_width": 1.5, "font": {"color": "#ffffff", "face": "ui-monospace, monospace", "size": 11, "bold": True},
    },
}


def _make_vis_edge(
    u: str,
    v: str,
    label: str,
    title: Optional[str] = None,
    dashes: Optional[Any] = None,
    color: Optional[Any] = None,
    font_color: Optional[str] = None,
    extra_props: Optional[Dict[str, Any]] = None,
) -> dict:
    """Helper to format a Vis.js edge according to canonical design palette."""
    cfg = REL_SPECS.get(label, DEFAULT_REL_SPEC)

    if isinstance(color, dict):
        edge_color = dict(color)
        if "hover" not in edge_color and "highlight" in edge_color:
            edge_color["hover"] = edge_color["highlight"]
    elif isinstance(color, str):
        edge_color = {
            "color": color,
            "highlight": cfg.get("highlight", "#ffffff"),
            "hover": cfg.get("hover", cfg.get("highlight", "#ffffff")),
        }
    else:
        edge_color = {
            "color": cfg.get("color", "#64748b"),
            "highlight": cfg.get("highlight", "#94a3b8"),
            "hover": cfg.get("hover", cfg.get("highlight", "#94a3b8")),
        }

    edge_font_color = font_color or cfg.get("font_color") or cfg.get("font", "#94a3b8")

    edge = {
        "from": u,
        "to": v,
        "label": label,
        "title": title or f"<b>{label}</b><br/>{Path(u).name if '/' in u else u} &rarr; {Path(v).name if '/' in v else v}",
        "arrows": {"to": {"enabled": True, "scaleFactor": 0.75}},
        "smooth": {"type": "continuous", "roundness": 0.28},
        "width": cfg.get("width", 1.8),
        "font": {
            "size": 9,
            "align": "middle",
            "color": edge_font_color,
            "face": "ui-monospace, monospace",
            "background": cfg.get("bg", "rgba(15, 23, 42, 0.94)"),
        },
        "color": edge_color,
    }
    actual_dashes = dashes if dashes is not None else cfg.get("dashes")
    if actual_dashes:
        edge["dashes"] = actual_dashes

    if extra_props:
        edge.update(extra_props)

    return edge


def _make_vis_node(
    node_id: str,
    label: str,
    group: Optional[str] = None,
    level: Optional[int] = None,
    color: Optional[Any] = None,
    shadow: Optional[Any] = None,
    title: Optional[str] = None,
    margin: Optional[int] = None,
    border_width: Optional[float] = None,
    font: Optional[Any] = None,
    shape: Optional[str] = None,
    shape_properties: Optional[Dict[str, Any]] = None,
    full_name: Optional[str] = None,
    extra_props: Optional[Dict[str, Any]] = None,
) -> dict:
    """Helper to format a Vis.js node according to canonical design palette."""
    lvl_cfg = NODE_LEVEL_SPECS.get(level, {}) if level is not None else {}

    node: Dict[str, Any] = {
        "id": node_id,
        "label": label,
        "full_name": full_name if full_name is not None else node_id,
    }
    if group is not None:
        node["group"] = group
    if title is not None:
        node["title"] = title
    if level is not None:
        node["level"] = level

    actual_color = color if color is not None else lvl_cfg.get("color")
    if actual_color is not None:
        node["color"] = actual_color

    actual_shape = shape if shape is not None else lvl_cfg.get("shape")
    if actual_shape is not None:
        node["shape"] = actual_shape

    actual_shape_props = shape_properties if shape_properties is not None else lvl_cfg.get("shape_properties")
    if actual_shape_props is not None:
        node["shapeProperties"] = actual_shape_props
    elif actual_shape == "box":
        node["shapeProperties"] = {"borderRadius": 6}

    actual_margin = margin if margin is not None else lvl_cfg.get("margin")
    if actual_margin is not None:
        node["margin"] = actual_margin

    actual_bw = border_width if border_width is not None else lvl_cfg.get("border_width")
    if actual_bw is not None:
        node["borderWidth"] = actual_bw

    actual_shadow = shadow if shadow is not None else lvl_cfg.get("shadow")
    if actual_shadow is not None:
        node["shadow"] = actual_shadow

    actual_font = font if font is not None else lvl_cfg.get("font")
    if actual_font is not None:
        node["font"] = actual_font

    if extra_props:
        node.update(extra_props)

    return node


REL_SPECS: Dict[str, dict] = {
    "DEFINED_IN": {
        "width": 1.8,
        "color": "#06b6d4",
        "highlight": "#22d3ee",
        "hover": "#38bdf8",
        "font_color": "#38bdf8",
        "font": "#38bdf8",
        "bg": "rgba(8, 20, 36, 0.94)",
        "dashes": False,
        "query": "MATCH (s:Symbol)-[d:DEFINED_IN]->(f:File) RETURN s.name, f.path, d.kind, d.line",
        "handler": None,
    },
    "REFERENCES": {
        "width": 2.0,
        "color": "#10b981",
        "highlight": "#34d399",
        "hover": "#6ee7b7",
        "font_color": "#34d399",
        "font": "#34d399",
        "bg": "rgba(6, 28, 20, 0.94)",
        "dashes": False,
        "query": "MATCH (caller:File)-[r:REFERENCES]->(s:Symbol)-[:DEFINED_IN]->(target:File) RETURN caller.path, s.name, r.line",
        "handler": None,
    },
    "IMPORTS": {
        "width": 1.8,
        "color": "#8b5cf6",
        "highlight": "#a78bfa",
        "hover": "#c4b5fd",
        "font_color": "#a78bfa",
        "font": "#a78bfa",
        "bg": "rgba(22, 14, 46, 0.94)",
        "dashes": False,
        "query": "MATCH (f:File)-[r:IMPORTS]->(s:Symbol) WHERE (s)-[:DEFINED_IN]->() RETURN f.path, s.name, r.line",
        "handler": None,
    },
    "IMPLEMENTS": {
        "width": 2.0,
        "color": "#ec4899",
        "highlight": "#f472b6",
        "hover": "#fbcfe8",
        "font_color": "#f472b6",
        "font": "#f472b6",
        "bg": "rgba(38, 10, 24, 0.94)",
        "dashes": [6, 4],
        "query": "MATCH (s1:Symbol)-[r:IMPLEMENTS]->(s2:Symbol) RETURN s1.name, s2.name",
        "handler": None,
        "src_role": "Class",
        "dst_role": "Interface",
    },
    "TYPE_OF": {
        "width": 1.8,
        "color": "#f97316",
        "highlight": "#fb923c",
        "hover": "#fed7aa",
        "font_color": "#fb923c",
        "font": "#fb923c",
        "bg": "rgba(36, 16, 6, 0.94)",
        "dashes": [3, 4],
        "query": "MATCH (s1:Symbol)-[r:TYPE_OF]->(s2:Symbol) RETURN s1.name, s2.name",
        "handler": None,
        "src_role": "",
        "dst_role": "Type",
    },
    "LINKS_TO": {
        "width": 1.8,
        "color": "#14b8a6",
        "highlight": "#2dd4bf",
        "hover": "#2dd4bf",
        "font_color": "#14b8a6",
        "font": "#14b8a6",
        "bg": "rgba(10, 28, 26, 0.94)",
        "dashes": False,
        "query": (
            "MATCH (d:Document)-[r:LINKS_TO]->(target) "
            "RETURN d.path, coalesce(d.title, d.path), coalesce(target.path, target.name), "
            "coalesce(target.title, target.name, target.path), labels(target), coalesce(r.label, 'LINKS_TO'), coalesce(r.link_type, 'link')"
        ),
        "handler": None,
    },
    "TAG": {
        "width": 1.5,
        "color": "#f97316",
        "highlight": "#fb923c",
        "hover": "#fb923c",
        "font_color": "#fed7aa",
        "font": "#fed7aa",
        "bg": "rgba(42, 20, 6, 0.94)",
        "dashes": [2, 2],
        "query": "MATCH (d:Document)-[r:HAS_TAG]->(t:Tag) RETURN d.path, coalesce(d.title, d.path), t.name",
        "handler": None,
    },
    "RELATES_TO": {
        "width": 1.8,
        "color": "#0ea5e9",
        "highlight": "#38bdf8",
        "hover": "#7dd3fc",
        "font_color": "#38bdf8",
        "font": "#38bdf8",
        "bg": "rgba(8, 24, 40, 0.94)",
        "dashes": False,
    },
    "BELONGS_TO": {
        "width": 1.8,
        "color": "#a855f7",
        "highlight": "#c084fc",
        "hover": "#d8b4fe",
        "font_color": "#c084fc",
        "font": "#c084fc",
        "bg": "rgba(28, 10, 42, 0.94)",
        "dashes": [4, 4],
        "query": "MATCH (f:File)-[r:BELONGS_TO]->(sub:Subsystem) RETURN f.path, sub.id, sub.name, coalesce(sub.description, '')",
        "handler": None,
    },
    "SUPERSEDES": {
        "width": 2.2,
        "color": "#ef4444",
        "highlight": "#f87171",
        "hover": "#fca5a5",
        "font_color": "#f87171",
        "font": "#f87171",
        "bg": "rgba(40, 8, 8, 0.94)",
        "dashes": [6, 3],
    },
    "MENTIONS": {
        "width": 1.2,
        "color": "#475569",
        "highlight": "#94a3b8",
        "hover": "#94a3b8",
        "font_color": "#94a3b8",
        "font": "#94a3b8",
        "bg": "rgba(15, 23, 42, 0.94)",
        "dashes": [4, 4],
    },
}

EDGE_STYLES = REL_SPECS

