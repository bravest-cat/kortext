"""
Presentation and markdown formatting utilities for Kortext symbol analysis.
Decouples Knapsack progressive level-of-detail token budgeting and markdown report
generation from underlying graph storage engines.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

__all__ = [
    "format_impact_report",
]


def format_impact_report(
    clean_name: str,
    direction: str,
    depth: int,
    definitions: list,
    direct_impacts: list,
    type_hierarchies: list,
    transitive_impacts: list,
    token_budget: Optional[int] = 1500,
    detail_level: str = "skeleton",
) -> dict:
    """
    Applies dynamic progressive level-of-detail (0/1 Knapsack style) and formats a markdown summary
    for symbol blast radius analysis.

    - 1-hop callers / definitions: Full signature and file path (L1 detail)
    - 2-hop callers: Compact path:line (L0 detail)
    - 3+ hop or budget overflow: Compressed count summary with impacted file list

    Args:
        clean_name: Target symbol or document identifier.
        direction: 'upstream' or 'downstream'.
        depth: Traversal depth hops (1-5).
        definitions: List of definition dictionaries.
        direct_impacts: List of direct 1-hop impact node dictionaries.
        type_hierarchies: List of type hierarchy/implementation dictionaries.
        transitive_impacts: List of multi-hop transitive reference dictionaries.
        token_budget: Max approximate tokens for the returned impact trace (default 1500).
        detail_level: 'skeleton', 'interface', or 'full' detail representation.

    Returns:
        Structured dict with in-budget definitions, direct call sites/imports/links, type hierarchies,
        progressive degradation statistics, and formatted markdown summary.
    """
    total_locations = len(direct_impacts) + len(type_hierarchies) + len(transitive_impacts)
    budget = token_budget if (token_budget and token_budget > 0) else 1000000

    # Build formatted lines with adaptive detail
    lines: List[str] = [
        f"### Symbol blast radius: `{clean_name}` ({direction}, max depth {depth})",
        f"- Direct definitions: {len(definitions)}",
        f"- Total affected sites: {total_locations}",
    ]

    included_definitions: List[Any] = []
    if definitions:
        lines.append("\n**Definitions:**")
        for d in definitions:
            sig_part = f" (`{d['signature']}`)" if d.get("signature") else ""
            def_line = f"- `{d.get('name', clean_name)}` ({d.get('kind', 'symbol')}) at `{d.get('file_path')}:{d.get('line', 1)}`{sig_part}"
            if detail_level == "full" and d.get("code"):
                lines.append(def_line)
                lines.append(f"```\n{d['code']}\n```")
            elif detail_level == "interface" and (d.get("methods") or d.get("code")):
                lines.append(def_line)
                code_snippet = d.get("code") or "\n".join(d.get("methods", []))
                if code_snippet:
                    lines.append(f"```\n{code_snippet}\n```")
            elif d.get("docstring"):
                lines.append(f"{def_line} - *\"{d['docstring']}\"*")
            else:
                lines.append(def_line)
            included_definitions.append(d)

    l1_count = 0
    l0_count = 0
    aggregated_count = 0
    omitted_files: set = set()

    def est_tokens(text_block: str) -> int:
        return max(1, len(text_block) // 4)

    current_tokens = est_tokens("\n".join(lines))

    # 1-hop Direct impacts: L1 detail
    included_direct: List[Any] = []
    if direct_impacts:
        lines.append("\n**Direct impacts (1-hop):**")
        for node in direct_impacts:
            node_line = f"- `{node['source_file']}:{node['line']}` [{node['relation']}] -> `{node['target_symbol']}`"
            line_tokens = est_tokens(node_line)
            if current_tokens + line_tokens < int(budget * 0.70):
                lines.append(node_line)
                current_tokens += line_tokens
                l1_count += 1
                included_direct.append(node)
            else:
                aggregated_count += 1
                omitted_files.add(Path(node['source_file']).name)

    # Transitive paths (Hierarchy / Implements)
    included_hierarchies: List[Any] = []
    if type_hierarchies:
        lines.append("\n**Type hierarchies & implementations:**")
        for tp in type_hierarchies:
            tp_line = f"- `{tp['symbol']}` [{tp['relation']}] -> `{tp.get('file_path', 'unknown')}`"
            line_tokens = est_tokens(tp_line)
            if current_tokens + line_tokens < int(budget * 0.85):
                lines.append(tp_line)
                current_tokens += line_tokens
                l1_count += 1
                included_hierarchies.append(tp)
            else:
                omitted_files.add(Path(tp.get('file_path', 'unknown')).name)
                aggregated_count += 1

    # Multi-hop transitive callers: L0 or aggregated count
    included_transitive: List[Any] = []
    if transitive_impacts:
        lines.append(f"\n**Transitive references ({2}..{depth} hops):**")
        for tn in transitive_impacts:
            tn_line = f"- `{tn['source_file']}` (depth {tn['depth']}) -> `{tn['symbol']}`"
            line_tokens = est_tokens(tn_line)
            if current_tokens + line_tokens < budget:
                lines.append(tn_line)
                current_tokens += line_tokens
                l0_count += 1
                included_transitive.append(tn)
            else:
                aggregated_count += 1
                omitted_files.add(Path(tn['source_file']).name)

    if aggregated_count > 0:
        sample_omitted = sorted(omitted_files)[:5]
        summary_tail = f"\n*({aggregated_count} additional impact sites in {len(omitted_files)} files omitted to fit token budget: {', '.join(sample_omitted)}...)*"
        lines.append(summary_tail)
        current_tokens += est_tokens(summary_tail)

    formatted_summary = "\n".join(lines)
    budget_exceeded = (aggregated_count > 0)

    return {
        "symbol": clean_name,
        "direction": direction.lower(),
        "max_depth": depth,
        "detail_level": detail_level,
        "definitions": included_definitions,
        "direct_impacts": included_direct,
        "type_hierarchies": included_hierarchies,
        "transitive_impacts": included_transitive,
        "omitted_files": sorted(omitted_files),
        "total_impacted_locations": total_locations,
        "token_budget": token_budget,
        "effective_tokens": current_tokens,
        "budget_exceeded": budget_exceeded,
        "detail_levels_applied": {
            "l1_full": l1_count,
            "l0_compact": l0_count,
            "aggregated_count": aggregated_count,
        },
        "formatted_summary": formatted_summary,
    }
