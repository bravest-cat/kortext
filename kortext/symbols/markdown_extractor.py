"""
Markdown AST, Concept & Link Extractor for Kortext.

Extracts structured knowledge from Markdown documents and Knowledge Vaults (Obsidian, Roam, RFCs):
1. YAML frontmatter metadata (title, tags, aliases, author, status)
2. Hierarchical section & heading tree (H1-H6 with parent breadcrumbs)
3. Obsidian-style Wiki-links ([[Note]], [[Note#Section]], [[Note|Anchor]])
4. Standard Markdown hyperlinks ([Anchor](relative/path.md))
5. Inline & frontmatter tags (#concept, #sub/tag)
6. Fenced code blocks with language metadata
"""
from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    yaml = None


class MarkdownSymbolExtractor:
    """Extracts semantic document structures, heading symbols, links, and tags from Markdown."""

    # Regex patterns
    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
    HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    WIKI_LINK_PATTERN = re.compile(r"!?\[\[([^\[\]]+)\]\]")
    MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    INLINE_TAG_PATTERN = re.compile(r"(?:^|\s)#([a-zA-Z0-9_\-\/]+)\b")
    CODE_BLOCK_PATTERN = re.compile(r"^```([a-zA-Z0-9_\-\+]*)\n(.*?)```", re.DOTALL | re.MULTILINE)

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None

    def extract(self, content: str, file_path: str) -> Dict[str, Any]:
        """
        Parses full markdown document into structured symbols, sections, links, and tags.
        """
        p = Path(file_path).resolve()
        doc_dir = p.parent

        # 1. Parse YAML frontmatter
        frontmatter, clean_body, fm_lines = self._parse_frontmatter(content)

        # 2. Extract Document Title
        title = frontmatter.get("title")
        if not title:
            # Check first H1 in content
            h1_match = re.search(r"^#\s+(.+)$", clean_body, re.MULTILINE)
            if h1_match:
                title = h1_match.group(1).strip()
            else:
                title = p.stem.replace("-", " ").replace("_", " ").title()
        title = str(title).strip()

        # 3. Extract Tags (Frontmatter + Inline)
        tags: Set[str] = set()
        fm_tags = frontmatter.get("tags") or frontmatter.get("tag") or []
        if isinstance(fm_tags, list):
            for t in fm_tags:
                tag_str = str(t).strip().lstrip("#")
                if tag_str:
                    tags.add(tag_str)
        elif isinstance(fm_tags, str):
            for t in re.split(r"[,;\s]+", fm_tags):
                tag_str = t.strip().lstrip("#")
                if tag_str:
                    tags.add(tag_str)

        # Extract inline tags (stripping code blocks first to avoid false positives)
        body_no_code = self.CODE_BLOCK_PATTERN.sub("", clean_body)
        for match in self.INLINE_TAG_PATTERN.finditer(body_no_code):
            tag_name = match.group(1).strip()
            # Ignore pure numeric (e.g. #123) or hex colors (e.g. #fff, #38bdf8)
            if re.match(r"^[0-9]+$", tag_name):
                continue
            if re.match(r"^[0-9a-fA-F]{3,6}$", tag_name) and len(tag_name) in (3, 6):
                continue
            tags.add(tag_name)

        # 4. Extract Headings and Section Hierarchy
        sections, declarations = self._extract_headings_and_sections(content, title, str(p))

        # 5. Extract Links (Wiki-links + Relative Markdown links)
        links = self._extract_links(clean_body, doc_dir, fm_lines)

        # 6. Extract Code Blocks
        code_blocks = self._extract_code_blocks(content)

        return {
            "file_path": str(p),
            "title": title,
            "frontmatter": frontmatter,
            "tags": sorted(list(tags)),
            "sections": sections,
            "declarations": declarations,
            "links": links,
            "code_blocks": code_blocks,
        }

    def _parse_frontmatter(self, content: str) -> Tuple[Dict[str, Any], str, int]:
        """Extracts and parses YAML frontmatter if present at the top of the file."""
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            return {}, content, 0

        fm_text = match.group(1)
        fm_lines = len(match.group(0).splitlines())
        clean_body = content[match.end():]
        parsed: Dict[str, Any] = {}

        if yaml:
            try:
                data = yaml.safe_load(fm_text)
                if isinstance(data, dict):
                    parsed = data
            except Exception:
                logger.debug("Suppressed non-fatal exception")
        else:
            # Fallback key-value parser if PyYAML unavailable
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    parsed[k.strip()] = v.strip().strip("\"'")

        return parsed, clean_body, fm_lines

    def _extract_headings_and_sections(
        self,
        content: str,
        doc_title: str,
        file_path: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Parses H1-H6 headers, building a nested hierarchy breadcrumb
        (e.g., 'DocTitle > Architecture > FalkorDB Integration').
        """
        lines = content.splitlines()
        sections: List[Dict[str, Any]] = []
        declarations: List[Dict[str, Any]] = []

        # Stack: list of (level, heading_title)
        heading_stack: List[Tuple[int, str]] = []

        for line_idx, line in enumerate(lines, start=1):
            h_match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if not h_match:
                continue

            level = len(h_match.group(1))
            heading_text = h_match.group(2).strip()

            # Pop deeper or equal levels
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()

            parent_scope = heading_stack[-1][1] if heading_stack else doc_title
            heading_stack.append((level, heading_text))

            # Full breadcrumb path (avoid duplicate title if H1 matches doc_title)
            if heading_stack and heading_stack[0][1].strip().lower() == doc_title.strip().lower():
                breadcrumb_parts = [item[1] for item in heading_stack]
            else:
                breadcrumb_parts = [doc_title] + [item[1] for item in heading_stack]
            breadcrumb_path = " > ".join(breadcrumb_parts)

            slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", heading_text.lower()).strip("-")
            section_id = f"{file_path}#{slug}" if slug else f"{file_path}#line-{line_idx}"

            sec_dict = {
                "id": section_id,
                "name": heading_text,
                "level": level,
                "line": line_idx,
                "slug": slug,
                "parent_scope": parent_scope,
                "heading_path": breadcrumb_path,
            }
            sections.append(sec_dict)

            # Treated as a Symbol declaration for Tier 0 resolution
            declarations.append({
                "name": heading_text,
                "kind": f"h{level}_section",
                "line": line_idx,
                "signature": breadcrumb_path,
                "docstring": f"Markdown section under {parent_scope}",
                "parent": parent_scope,
            })

        return sections, declarations

    def _extract_links(
        self,
        clean_body: str,
        doc_dir: Path,
        line_offset: int,
    ) -> List[Dict[str, Any]]:
        """Extracts Wiki-links and standard Markdown links."""
        links: List[Dict[str, Any]] = []
        seen_targets: Set[Tuple[str, int]] = set()

        lines = clean_body.splitlines()

        for rel_line_idx, line in enumerate(lines, start=1):
            line_idx = rel_line_idx + line_offset

            # 1. Wiki-links: [[TargetDoc]], [[TargetDoc#Section]], [[TargetDoc|Anchor Text]]
            for w_match in self.WIKI_LINK_PATTERN.finditer(line):
                raw_inner = w_match.group(1).strip()
                if not raw_inner:
                    continue

                anchor_label = ""
                sub_section = ""
                target_name = raw_inner

                if "|" in target_name:
                    target_name, anchor_label = target_name.split("|", 1)
                    target_name = target_name.strip()
                    anchor_label = anchor_label.strip()

                if "#" in target_name:
                    target_name, sub_section = target_name.split("#", 1)
                    target_name = target_name.strip()
                    sub_section = sub_section.strip()

                if not target_name:
                    continue

                resolved_path, is_resolved = self._resolve_target(target_name, doc_dir)
                key = (resolved_path or target_name, line_idx)
                if key not in seen_targets:
                    seen_targets.add(key)
                    links.append({
                        "link_type": "wiki_link",
                        "raw_target": raw_inner,
                        "target_name": target_name,
                        "target_path": resolved_path,
                        "is_resolved": is_resolved,
                        "sub_section": sub_section,
                        "label": anchor_label or target_name,
                        "line": line_idx,
                    })

            # 2. Standard Markdown links: [Anchor](path/to/target.md)
            for m_match in self.MD_LINK_PATTERN.finditer(line):
                label = m_match.group(1).strip()
                url_target = m_match.group(2).strip()

                # Ignore external URLs, emails, internal page anchors (#only)
                if url_target.startswith(("http://", "https://", "mailto:", "ftp://")):
                    continue
                if url_target.startswith("#"):
                    continue

                sub_section = ""
                clean_target = url_target
                if "#" in clean_target:
                    clean_target, sub_section = clean_target.split("#", 1)

                resolved_path, is_resolved = self._resolve_target(clean_target, doc_dir)
                key = (resolved_path or clean_target, line_idx)
                if key not in seen_targets:
                    seen_targets.add(key)
                    links.append({
                        "link_type": "markdown_link",
                        "raw_target": url_target,
                        "target_name": Path(clean_target).stem,
                        "target_path": resolved_path,
                        "is_resolved": is_resolved,
                        "sub_section": sub_section,
                        "label": label or clean_target,
                        "line": line_idx,
                    })

        return links

    def _resolve_target(self, target: str, current_dir: Path) -> Tuple[Optional[str], bool]:
        """
        Attempts to resolve target note or file to a real filesystem path.
        Returns: (resolved_absolute_path_or_none, is_resolved_bool)
        """
        # 1. Direct path relative to current file's directory
        candidates = [
            current_dir / target,
            current_dir / f"{target}.md",
            current_dir / f"{target}.markdown",
        ]

        # 2. Path relative to workspace root (if known)
        if self.workspace_root:
            candidates.extend([
                self.workspace_root / target,
                self.workspace_root / f"{target}.md",
                self.workspace_root / f"{target}.markdown",
            ])

        for c in candidates:
            try:
                if c.is_file():
                    return str(c.resolve()), True
            except Exception:
                logger.debug("Suppressed non-fatal exception")

        # 3. If workspace_root is present, search for matching filename
        if self.workspace_root and not ("/" in target or "\\" in target):
            target_stem = Path(target).stem.lower()
            try:
                for match_file in self.workspace_root.rglob(f"*.md"):
                    if match_file.stem.lower() == target_stem:
                        return str(match_file.resolve()), True
            except Exception:
                logger.debug("Suppressed non-fatal exception")

        return None, False

    def _extract_code_blocks(self, content: str) -> List[Dict[str, Any]]:
        """Finds all code blocks with language and line positions."""
        blocks = []
        for m in self.CODE_BLOCK_PATTERN.finditer(content):
            lang = m.group(1).strip()
            code_text = m.group(2)
            start_pos = m.start()
            line_idx = content[:start_pos].count("\n") + 1
            blocks.append({
                "language": lang or "text",
                "line": line_idx,
                "line_count": len(code_text.splitlines()),
            })
        return blocks
