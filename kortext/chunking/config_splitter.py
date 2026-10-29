"""
Structured Configuration File Splitter for Kortext (.yaml, .yml, .json, .toml).
"""
from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

import json
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    import tomllib
    _TOMLLIB_AVAILABLE = True
except ImportError:
    _TOMLLIB_AVAILABLE = False


from kortext.chunking.token_utils import count_tokens


class ConfigSectionSplitter:
    """
    Parses structured configuration files (.yaml, .yml, .toml, .json)
    by logical sections or keys, with syntax verification and breadcrumbs.
    """

    def __init__(self, max_tokens: int = 384):
        self.max_tokens = max_tokens

    def split_config(self, text: str, file_path: str = "") -> List[Dict[str, Any]]:
        ext = Path(file_path).suffix.lower()
        chunks: List[Dict[str, Any]] = []

        if ext == ".json":
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return self._split_dict_sections(data, file_path, "json")
            except Exception:
                logger.debug("Suppressed non-fatal exception")
        elif ext in (".yaml", ".yml") and _YAML_AVAILABLE:
            try:
                data = yaml.safe_load(text)
                if isinstance(data, dict):
                    return self._split_dict_sections(data, file_path, "yaml")
            except Exception:
                logger.debug("Suppressed non-fatal exception")
        elif ext == ".toml" and _TOMLLIB_AVAILABLE:
            try:
                data = tomllib.loads(text)
                if isinstance(data, dict):
                    return self._split_dict_sections(data, file_path, "toml")
            except Exception:
                logger.debug("Suppressed non-fatal exception")

        # Unparsed text: windows of whole lines, each with its true character offsets.
        header = f"# File: {file_path}\n# Config File\n\n"
        chunks: List[Dict[str, Any]] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + 2048)
            if end < len(text):
                nl = text.rfind("\n", start, end)
                end = nl + 1 if nl > start else end
            chunks.append({
                "text": header + text[start:end],
                "scope": "config",
                "chunk_type": "config_section",
                "start_char": start,
                "end_char": end,
            })
            start = end
        return chunks or [{"text": header, "scope": "config", "chunk_type": "config_section", "start_char": 0, "end_char": 0}]

    def _split_dict_sections(self, data: Dict[str, Any], file_path: str, fmt: str) -> List[Dict[str, Any]]:
        chunks = []
        buffered_items: Dict[str, Any] = {}
        buffered_tokens = 0

        def flush_config():
            nonlocal buffered_items, buffered_tokens
            if not buffered_items:
                return
            keys = list(buffered_items.keys())
            scope_str = f"config sections: {', '.join(keys[:3])}"
            if len(keys) > 3:
                scope_str += f" (+{len(keys) - 3} more)"
            header = f"# File: {file_path}\n# Scope: {scope_str}\n\n"

            if fmt == "json":
                body = json.dumps(buffered_items, indent=2)
            elif fmt == "yaml" and _YAML_AVAILABLE:
                body = yaml.dump(buffered_items, default_flow_style=False)
            else:
                body = str(buffered_items)

            chunks.append({
                "text": header + body,
                "scope": scope_str,
                "chunk_type": "config_section",
                "start_char": 0,
                "end_char": len(body),
            })
            buffered_items = {}
            buffered_tokens = 0

        for k, v in data.items():
            if fmt == "json":
                item_str = json.dumps({k: v}, indent=2)
            elif fmt == "yaml" and _YAML_AVAILABLE:
                item_str = yaml.dump({k: v}, default_flow_style=False)
            else:
                item_str = f"{k} = {v}"

            item_toks = count_tokens(item_str)
            if item_toks > self.max_tokens:
                flush_config()
                header = f"# File: {file_path}\n# Scope: section [{k}]\n\n"
                chunks.append({
                    "text": header + item_str[:2048],
                    "scope": f"section [{k}]",
                    "chunk_type": "config_section",
                    "start_char": 0,
                    "end_char": len(item_str),
                })
            elif buffered_tokens + item_toks > self.max_tokens and buffered_items:
                flush_config()
                buffered_items[k] = v
                buffered_tokens = item_toks
            else:
                buffered_items[k] = v
                buffered_tokens += item_toks

        flush_config()
        return chunks
