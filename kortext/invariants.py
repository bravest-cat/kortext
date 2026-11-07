"""
AST Grounding Hashes and Invariant Drift Detection (The MEX Pattern).

Grounds abstract architectural rules and invariants to concrete code symbols
and their AST body SHA-256 hashes:
    Invariant(R) grounds_to (symbol_id, SHA256(AST_body))

When code under the invariant changes, the AST body hash diverges, flagging
the invariant as DRIFTED / STALE.
"""
from __future__ import annotations

import ast as py_ast
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from kortext.chunking.ast_extractor import get_body_node, get_identifier
    from kortext.chunking.languages import LANG_MAP, Parser
except Exception:
    get_body_node = None  # type: ignore
    get_identifier = None  # type: ignore
    LANG_MAP = {}  # type: ignore
    Parser = None  # type: ignore
from kortext._home import get_kortext_home
from kortext.falkordb_client import (
    FALKORDB_HOST,
    FALKORDB_PASS,
    FALKORDB_PORT,
    FalkorDBClient,
    get_shared_falkordb_client,
)

KORTEXT_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", str(KORTEXT_ROOT / "journal")))
# Runtime writes go to the Kortext home so the tracked repo copy stays clean.
_DEFAULT_GROUNDED_FILE = (
    Path(os.environ["JOURNAL_DIR"]) / "grounded_invariants.json"
    if os.getenv("JOURNAL_DIR")
    else get_kortext_home() / "journal" / "grounded_invariants.json"
)
GROUNDED_FILE = _DEFAULT_GROUNDED_FILE
REPO_GROUNDED_FILE = KORTEXT_ROOT / "journal" / "grounded_invariants.json"
# Same directory the sanitizer writes and the dashboard reads.
QUARANTINE_DIR = (
    Path(os.environ["JOURNAL_DIR"]) / "quarantine" if os.getenv("JOURNAL_DIR") else get_kortext_home() / "quarantine"
)
FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))


def grounded_read_path() -> Path:
    """GROUNDED_FILE, or the repo copy when the default home copy does not exist yet."""
    if GROUNDED_FILE == _DEFAULT_GROUNDED_FILE and not GROUNDED_FILE.is_file():
        return REPO_GROUNDED_FILE
    return GROUNDED_FILE


def _resolve_invariant_file(file_path: str) -> Path:
    """Resolves an invariant file path (absolute or repo-relative) against KORTEXT_ROOT."""
    raw = Path(file_path)
    if raw.is_absolute() and raw.exists():
        return raw.resolve()
    candidate = (KORTEXT_ROOT / raw).resolve()
    if candidate.exists():
        return candidate
    return raw.resolve()


def _parse_symbol_target(symbol_name: str) -> Tuple[Optional[str], str]:
    """Splits optional 'ClassName.method_name' or 'ClassName::method_name' into (parent_lower, target_lower)."""
    cleaned = symbol_name.replace("::", ".").strip().lower()
    if "." in cleaned:
        parts = [p.strip() for p in cleaned.split(".") if p.strip()]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return None, cleaned


def _normalize_body_slice(body_bytes: bytes, is_python: bool = False) -> bytes:
    """Normalizes AST body bytes identically across Tree-sitter and Python AST fallback."""
    if is_python:
        cleaned_lines = []
        for line in body_bytes.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(b"#"):
                continue
            cleaned_lines.append(line)
        body_bytes = b"\n".join(cleaned_lines)
    return b" ".join(body_bytes.split())


def _py_ast_body_hash(p: Path, symbol_name: str) -> Optional[str]:
    """Fallback Python AST byte-slice body hasher producing bit-identical hashes to Tree-sitter."""
    try:
        src_bytes = p.read_bytes()
        lines = src_bytes.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        tree = py_ast.parse(src_bytes)
        target_parent, target_name = _parse_symbol_target(symbol_name)

        def _extract_hash(node: Any) -> Optional[str]:
            if not getattr(node, "body", None):
                return None
            start_byte = offsets[node.body[0].lineno - 1] + node.body[0].col_offset
            end_byte = offsets[node.end_lineno - 1] + node.end_col_offset
            body_bytes = src_bytes[start_byte:end_byte]
            return hashlib.sha256(_normalize_body_slice(body_bytes, is_python=True)).hexdigest()

        stack: List[Tuple[py_ast.AST, Optional[str]]] = [(tree, None)]
        while stack:
            curr, parent_cls = stack.pop()
            if isinstance(curr, (py_ast.FunctionDef, py_ast.AsyncFunctionDef, py_ast.ClassDef)):
                curr_name = curr.name.lower()
                if curr_name == target_name and (target_parent is None or parent_cls == target_parent):
                    res = _extract_hash(curr)
                    if res is not None:
                        return res
                next_cls = curr_name if isinstance(curr, py_ast.ClassDef) else parent_cls
            else:
                next_cls = parent_cls

            for child in reversed(list(py_ast.iter_child_nodes(curr))):
                stack.append((child, next_cls))
    except Exception as e:
        logger.debug("Python AST fallback hash failed: %s", e)
    return None



def _py_ast_signature_hash(p: Path, symbol_name: str) -> Optional[str]:
    """Computes a normalized SHA-256 hash of a Python function/method/class signature + return annotation."""
    try:
        src_bytes = p.read_bytes()
        tree = py_ast.parse(src_bytes)
        target_parent, target_name = _parse_symbol_target(symbol_name)

        def _format_sig(node: Any) -> Optional[str]:
            if isinstance(node, (py_ast.FunctionDef, py_ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(node, py_ast.AsyncFunctionDef) else "def"
                args_str = py_ast.unparse(node.args) if hasattr(py_ast, "unparse") else node.name
                ret_str = f" -> {py_ast.unparse(node.returns)}" if (getattr(node, "returns", None) and hasattr(py_ast, "unparse")) else ""
                sig_text = f"{prefix} {node.name}({args_str}){ret_str}"
                return hashlib.sha256(b" ".join(sig_text.encode("utf-8").split())).hexdigest()
            if isinstance(node, py_ast.ClassDef):
                bases = [py_ast.unparse(b) for b in node.bases] if hasattr(py_ast, "unparse") else []
                sig_text = f"class {node.name}({', '.join(bases)})"
                return hashlib.sha256(b" ".join(sig_text.encode("utf-8").split())).hexdigest()
            return None

        stack: List[Tuple[py_ast.AST, Optional[str]]] = [(tree, None)]
        while stack:
            curr, parent_cls = stack.pop()
            if isinstance(curr, (py_ast.FunctionDef, py_ast.AsyncFunctionDef, py_ast.ClassDef)):
                curr_name = curr.name.lower()
                if curr_name == target_name and (target_parent is None or parent_cls == target_parent):
                    res = _format_sig(curr)
                    if res is not None:
                        return res
                next_cls = curr_name if isinstance(curr, py_ast.ClassDef) else parent_cls
            else:
                next_cls = parent_cls
            for child in reversed(list(py_ast.iter_child_nodes(curr))):
                stack.append((child, next_cls))
    except Exception as e:
        logger.debug("Python AST signature hash failed: %s", e)
    return None


def compute_ast_signature_hash(file_path: str, symbol_name: str) -> Optional[str]:
    """
    Hashes the AST declaration header (signature, parameters, and return type annotation)
    separately from the implementation body so internal refactors do not trigger false-positive drift.
    """
    p = _resolve_invariant_file(file_path)
    if not p.exists() or not p.is_file():
        return None

    ext = p.suffix.lower()
    if ext == ".py":
        sig_h = _py_ast_signature_hash(p, symbol_name)
        if sig_h:
            return sig_h

    lang = LANG_MAP.get(ext) if LANG_MAP else None
    if not lang or Parser is None:
        return None

    try:
        source_bytes = p.read_bytes()
        parser = Parser(lang)
        tree = parser.parse(source_bytes)
    except Exception:
        return None

    target_parent, target_name = _parse_symbol_target(symbol_name)
    stack: List[Tuple[Any, Optional[str]]] = [(tree.root_node, None)]
    matching_node = None

    while stack:
        curr, enclosing_cls = stack.pop()
        ident = get_identifier(curr, source_bytes)
        ident_lower = ident.lower() if ident else None
        is_container = any(k in curr.type for k in ("class", "struct", "impl", "interface"))
        is_def = is_container or any(k in curr.type for k in ("function", "method", "type"))

        if ident_lower == target_name and (target_parent is None or enclosing_cls == target_parent):
            if is_def:
                matching_node = curr
                break
            if matching_node is None:
                matching_node = curr

        child_cls = ident_lower if (is_container and ident_lower) else enclosing_cls
        for child in reversed(curr.children):
            stack.append((child, child_cls))

    if not matching_node:
        return None

    body = get_body_node(matching_node)
    if body and body.start_byte > matching_node.start_byte:
        header_bytes = source_bytes[matching_node.start_byte : body.start_byte]
    else:
        header_bytes = source_bytes[matching_node.start_byte : matching_node.end_byte].splitlines()[0]

    normalized_header = b" ".join(header_bytes.strip().rstrip(b":").rstrip(b"{").split())
    return hashlib.sha256(normalized_header).hexdigest()


def compute_ast_body_hash(file_path: str, symbol_name: str) -> Optional[str]:
    """
    Parses a source file with Tree-sitter (or Python AST fallback), finds the node for symbol_name
    (supporting qualified `ClassName.method_name`), and returns the SHA-256 hash of its normalized AST body bytes.
    """
    p = _resolve_invariant_file(file_path)
    if not p.exists() or not p.is_file():
        return None

    ext = p.suffix.lower()
    lang = LANG_MAP.get(ext) if LANG_MAP else None
    if not lang or Parser is None:
        if ext == ".py":
            return _py_ast_body_hash(p, symbol_name)
        return None

    try:
        source_bytes = p.read_bytes()
        parser = Parser(lang)
        tree = parser.parse(source_bytes)
    except Exception:
        if ext == ".py":
            return _py_ast_body_hash(p, symbol_name)
        return None

    target_parent, target_name = _parse_symbol_target(symbol_name)

    # Search DFS for symbol definition while tracking enclosing class/struct/impl scope
    stack: List[Tuple[Any, Optional[str]]] = [(tree.root_node, None)]
    matching_node = None

    while stack:
        curr, enclosing_cls = stack.pop()
        ident = get_identifier(curr, source_bytes)
        ident_lower = ident.lower() if ident else None
        is_container = any(k in curr.type for k in ("class", "struct", "impl", "interface"))
        is_def = is_container or any(k in curr.type for k in ("function", "method", "type"))

        if ident_lower == target_name and (target_parent is None or enclosing_cls == target_parent):
            if is_def:
                matching_node = curr
                break
            if matching_node is None:
                matching_node = curr

        child_cls = ident_lower if (is_container and ident_lower) else enclosing_cls
        for child in reversed(curr.children):
            stack.append((child, child_cls))

    if not matching_node:
        if ext == ".py":
            return _py_ast_body_hash(p, symbol_name)
        return None

    body = get_body_node(matching_node)
    if body:
        # Align start byte with first non-comment statement so Python AST and Tree-sitter match identically
        first_stmt = next((c for c in body.children if c.type != "comment"), body)
        body_bytes = source_bytes[first_stmt.start_byte : body.end_byte]
    else:
        body_bytes = source_bytes[matching_node.start_byte : matching_node.end_byte]

    normalized = _normalize_body_slice(body_bytes, is_python=(ext == ".py"))
    return hashlib.sha256(normalized).hexdigest()


def compute_file_ast_body_hashes(file_path: str) -> Dict[str, str]:
    """Computes AST body hashes for all symbols in a file from a single parse."""
    p = _resolve_invariant_file(file_path)
    if not p.exists() or not p.is_file():
        return {}

    ext = p.suffix.lower()
    hashes: Dict[str, str] = {}
    if ext == ".py":
        try:
            src_bytes = p.read_bytes()
            lines = src_bytes.splitlines(keepends=True)
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line))
            tree = py_ast.parse(src_bytes)

            def _hash_node(node: Any) -> Optional[str]:
                if not getattr(node, "body", None):
                    return None
                start_byte = offsets[node.body[0].lineno - 1] + node.body[0].col_offset
                end_byte = offsets[node.end_lineno - 1] + node.end_col_offset
                body_bytes = src_bytes[start_byte:end_byte]
                return hashlib.sha256(_normalize_body_slice(body_bytes, is_python=True)).hexdigest()

            stack: List[Tuple[py_ast.AST, Optional[str]]] = [(tree, None)]
            while stack:
                curr, parent_cls = stack.pop()
                if isinstance(curr, (py_ast.FunctionDef, py_ast.AsyncFunctionDef, py_ast.ClassDef)):
                    c_name = curr.name.lower()
                    h = _hash_node(curr)
                    if h:
                        if parent_cls:
                            hashes[f"{parent_cls}.{c_name}"] = h
                        if c_name not in hashes:
                            hashes[c_name] = h
                    next_cls = c_name if isinstance(curr, py_ast.ClassDef) else parent_cls
                else:
                    next_cls = parent_cls
                for child in reversed(list(py_ast.iter_child_nodes(curr))):
                    stack.append((child, next_cls))
            return hashes
        except Exception:
            return {}

    lang = LANG_MAP.get(ext) if LANG_MAP else None
    if not lang or Parser is None:
        return {}

    try:
        source_bytes = p.read_bytes()
        parser = Parser(lang)
        tree = parser.parse(source_bytes)
        stack: List[Tuple[Any, Optional[str]]] = [(tree.root_node, None)]
        while stack:
            curr, enclosing_cls = stack.pop()
            ident = get_identifier(curr, source_bytes)
            ident_lower = ident.lower() if ident else None
            is_container = any(k in curr.type for k in ("class", "struct", "impl", "interface"))
            is_def = is_container or any(k in curr.type for k in ("function", "method", "type"))
            if is_def and ident_lower:
                body = get_body_node(curr)
                if body:
                    first_stmt = next((c for c in body.children if c.type != "comment"), body)
                    body_bytes = source_bytes[first_stmt.start_byte : body.end_byte]
                else:
                    body_bytes = source_bytes[curr.start_byte : curr.end_byte]
                h = hashlib.sha256(_normalize_body_slice(body_bytes, is_python=False)).hexdigest()
                if enclosing_cls:
                    hashes[f"{enclosing_cls}.{ident_lower}"] = h
                if ident_lower not in hashes:
                    hashes[ident_lower] = h
            child_cls = ident_lower if (is_container and ident_lower) else enclosing_cls
            for child in reversed(curr.children):
                stack.append((child, child_cls))
        return hashes
    except Exception:
        return {}


def _extract_symbol_source_slice(file_path: str, symbol_name: str) -> Optional[str]:
    """
    Extracts the source slice belonging specifically to `symbol_name` (supporting `Class.method`)
    using Tree-sitter or Python AST fallback. Returns None if the symbol is not found in the file.
    """
    p = _resolve_invariant_file(file_path)
    if not p.exists() or not p.is_file():
        return None

    ext = p.suffix.lower()
    target_parent, target_name = _parse_symbol_target(symbol_name)

    try:
        src_bytes = p.read_bytes()
    except Exception:
        return None

    lang = LANG_MAP.get(ext) if LANG_MAP else None
    if lang and Parser is not None:
        try:
            parser = Parser(lang)
            tree = parser.parse(src_bytes)
            stack: List[Tuple[Any, Optional[str]]] = [(tree.root_node, None)]
            matching_node = None
            while stack:
                curr, enclosing_cls = stack.pop()
                ident = get_identifier(curr, src_bytes)
                ident_lower = ident.lower() if ident else None
                is_container = any(k in curr.type for k in ("class", "struct", "impl", "interface"))
                is_def = is_container or any(k in curr.type for k in ("function", "method", "type"))

                if ident_lower == target_name and (target_parent is None or enclosing_cls == target_parent):
                    if is_def:
                        matching_node = curr
                        break
                    if matching_node is None:
                        matching_node = curr

                child_cls = ident_lower if (is_container and ident_lower) else enclosing_cls
                for child in reversed(curr.children):
                    stack.append((child, child_cls))

            if matching_node is not None:
                body = get_body_node(matching_node)
                slice_node = body if body is not None else matching_node
                return src_bytes[slice_node.start_byte : slice_node.end_byte].decode("utf-8", errors="ignore")
        except Exception:
            pass

    if ext == ".py":
        try:
            lines = src_bytes.splitlines(keepends=True)
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line))
            tree = py_ast.parse(src_bytes)
            stack_py: List[Tuple[py_ast.AST, Optional[str]]] = [(tree, None)]
            while stack_py:
                curr, parent_cls = stack_py.pop()
                if isinstance(curr, (py_ast.FunctionDef, py_ast.AsyncFunctionDef, py_ast.ClassDef)):
                    curr_name = curr.name.lower()
                    if curr_name == target_name and (target_parent is None or parent_cls == target_parent):
                        if getattr(curr, "body", None):
                            start_b = offsets[curr.body[0].lineno - 1] + curr.body[0].col_offset
                            end_b = offsets[curr.end_lineno - 1] + curr.end_col_offset
                            return src_bytes[start_b:end_b].decode("utf-8", errors="ignore")
                    next_cls = curr_name if isinstance(curr, py_ast.ClassDef) else parent_cls
                else:
                    next_cls = parent_cls
                for child in reversed(list(py_ast.iter_child_nodes(curr))):
                    stack_py.append((child, next_cls))
        except Exception:
            pass

    return None


def compute_ast_minhash_sketch(file_path: str, symbol_name: str = "", num_hashes: int = 16) -> List[str]:
    """
    Computes a symbol-scoped structural AST token-trigram MinHash sketch for Tier-2 fuzzy invariant matching
    when minor variable renames, formatting edits, or cross-file symbol moves occur.
    Falls back to file-level tokens only when symbol_name is omitted or cannot be isolated.
    """
    p = _resolve_invariant_file(file_path)
    if not p.exists() or not p.is_file():
        return []
    try:
        src: Optional[str] = None
        if symbol_name and symbol_name.strip():
            src = _extract_symbol_source_slice(file_path, symbol_name)
        if not src:
            src = p.read_text(encoding="utf-8", errors="ignore")
        tokens = [
            t
            for t in src.replace("(", " ").replace(")", " ").replace(":", " ").replace("{", " ").replace("}", " ").split()
            if len(t) > 1
        ]
        if len(tokens) < 3:
            return [hashlib.md5(t.encode("utf-8")).hexdigest()[:8] for t in tokens][:num_hashes]
        trigrams = {f"{tokens[i]}|{tokens[i+1]}|{tokens[i+2]}" for i in range(len(tokens) - 2)}
        hashes = sorted(hashlib.md5(tg.encode("utf-8")).hexdigest()[:8] for tg in trigrams)
        return hashes[:num_hashes]
    except Exception:
        return []


def _minhash_jaccard(sketch_a: Optional[List[str]], sketch_b: Optional[List[str]]) -> float:
    if not sketch_a or not sketch_b:
        return 0.0
    sa, sb = set(sketch_a), set(sketch_b)
    union = len(sa | sb)
    return (len(sa & sb) / union) if union > 0 else 0.0


class InvariantGroundingEngine:
    """Manages invariant grounding, drift detection, and quarantine."""

    def __init__(
        self,
        host: str = FALKORDB_HOST,
        port: int = FALKORDB_PORT,
        password: Optional[str] = FALKORDB_PASS,
        db_name: str = FALKORDB_DB,
        client: Optional[FalkorDBClient] = None,
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.db_name = db_name
        self.client = (
            client
            if client is not None
            else get_shared_falkordb_client(host=self.host, port=self.port, password=self.password)
        )
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    def _query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[List[Any]]:
        return self.client.query(self.db_name, cypher, params)

    def load_grounded_invariants(self) -> List[Dict[str, Any]]:
        """Loads grounded invariant records from disk."""
        src = grounded_read_path()
        if not src.exists():
            return []
        try:
            with open(src, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def save_grounded_invariants(self, invariants: List[Dict[str, Any]]):
        """Saves grounded invariant records to disk."""
        GROUNDED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(GROUNDED_FILE, "w", encoding="utf-8") as f:
            json.dump(invariants, f, indent=2)

    def ground_invariant(
        self,
        domain: str,
        rule: str,
        rationale: str,
        symbol_name: str,
        file_path: str,
        trust_level: str = "speculative",
    ) -> Dict[str, Any]:
        """
        Grounds an architectural invariant to a live code symbol and its AST body hash.
        """
        now = datetime.now(timezone.utc).isoformat()
        resolved_path = str(Path(file_path).resolve())
        sig_hash = compute_ast_signature_hash(resolved_path, symbol_name)
        body_hash = compute_ast_body_hash(resolved_path, symbol_name)
        ast_hash = sig_hash or body_hash
        minhash_sketch = compute_ast_minhash_sketch(resolved_path, symbol_name)

        record = {
            "domain": domain,
            "rule": rule,
            "rationale": rationale,
            "symbol_name": symbol_name,
            "file_path": resolved_path,
            "signature_hash": sig_hash,
            "body_hash": body_hash,
            "ast_hash": ast_hash,
            "minhash_sketch": minhash_sketch,
            "status": "active" if ast_hash else "unresolved_symbol",
            "trust_level": trust_level,
            "grounded_at": now,
            "last_verified": now,
        }

        items = self.load_grounded_invariants()
        updated = False
        for i, item in enumerate(items):
            if item.get("rule") == rule:
                items[i] = record
                updated = True
                break
        if not updated:
            items.append(record)
        self.save_grounded_invariants(items)

        self._query(
            "MERGE (i:Invariant {rule: $rule}) "
            "SET i.domain = $domain, i.rationale = $rationale, i.symbol = $symbol, "
            "    i.file_path = $file, i.ast_hash = $hash, i.status = $status, "
            "    i.trust_level = $trust_level, i.updated_at = $now",
            {
                "rule": rule,
                "domain": domain,
                "rationale": rationale,
                "symbol": symbol_name,
                "file": resolved_path,
                "hash": ast_hash or "",
                "status": record["status"],
                "trust_level": trust_level,
                "now": now,
            },
        )

        return record

    def _try_relocate_symbol(
        self,
        symbol_name: str,
        old_file_path: str,
        recorded_sig_hash: Optional[str],
        recorded_body_hash: Optional[str],
        recorded_sketch: Optional[List[str]],
    ) -> Optional[Path]:
        """
        Searches sibling and workspace Python/code files when a symbol or file is moved/renamed,
        using exact AST signature/body hash or Tier-2 AST token-trigram MinHash similarity (>= 0.50).
        """
        old_p = Path(old_file_path)
        search_dirs: List[Path] = []
        if old_p.parent.is_dir():
            search_dirs.append(old_p.parent)
        if KORTEXT_ROOT.is_dir() and KORTEXT_ROOT not in search_dirs:
            search_dirs.append(KORTEXT_ROOT)

        ext = old_p.suffix.lower() or ".py"
        best_candidate: Optional[Path] = None
        best_sim = 0.0

        for sdir in search_dirs:
            pattern = f"*{ext}" if sdir == old_p.parent else f"kortext/**/*{ext}"
            for cand in list(sdir.glob(pattern))[:80]:
                if not cand.is_file() or cand.resolve() == old_p.resolve():
                    continue
                cand_str = str(cand.resolve())
                sig_h = compute_ast_signature_hash(cand_str, symbol_name)
                body_h = compute_ast_body_hash(cand_str, symbol_name)
                if (sig_h and recorded_sig_hash and sig_h == recorded_sig_hash) or (
                    body_h and recorded_body_hash and body_h == recorded_body_hash
                ):
                    return cand.resolve()
                if (sig_h or body_h) and recorded_sketch:
                    cand_sketch = compute_ast_minhash_sketch(cand_str, symbol_name)
                    sim = _minhash_jaccard(recorded_sketch, cand_sketch)
                    if sim >= 0.50 and sim > best_sim:
                        best_sim = sim
                        best_candidate = cand.resolve()
        return best_candidate

    def audit_invariant_drift(self, quarantine_drifted: bool = False, strict_body: bool = False) -> Dict[str, Any]:
        """
        Audits all grounded invariants against live codebase AST body hashes.
        Detects drifted, verified, relocated, and orphaned invariants.
        Flags gutted/stubbed bodies ('pass', '...', 'return True/False/None') or strict_body=True
        modifications as drifted even when the signature matches.
        """
        now = datetime.now(timezone.utc).isoformat()
        items = self.load_grounded_invariants()

        verified: List[Dict[str, Any]] = []
        drifted: List[Dict[str, Any]] = []
        orphaned: List[Dict[str, Any]] = []

        _TRIVIAL_STUB_BODIES = {
            hashlib.sha256(b"pass").hexdigest(),
            hashlib.sha256(b"...").hexdigest(),
            hashlib.sha256(b"return True").hexdigest(),
            hashlib.sha256(b"return False").hexdigest(),
            hashlib.sha256(b"return None").hexdigest(),
            hashlib.sha256(b"return").hexdigest(),
        }

        for item in items:
            file_path = item.get("file_path", "")
            symbol_name = item.get("symbol_name", "")
            recorded_hash = item.get("ast_hash")
            recorded_sig_hash = item.get("signature_hash")
            recorded_body_hash = item.get("body_hash") or recorded_hash
            recorded_sketch = item.get("minhash_sketch")
            item_strict = strict_body or bool(item.get("strict_body", False))

            p = _resolve_invariant_file(file_path)
            if not p.exists():
                relocated = self._try_relocate_symbol(
                    symbol_name, file_path, recorded_sig_hash, recorded_body_hash, recorded_sketch
                )
                if relocated is not None:
                    item["relocated_from"] = file_path
                    file_path = str(relocated)
                    item["file_path"] = file_path
                    p = relocated
                else:
                    item["status"] = "orphaned_file"
                    orphaned.append(item)
                    continue

            live_sig_hash = compute_ast_signature_hash(file_path, symbol_name)
            live_body_hash = compute_ast_body_hash(file_path, symbol_name)
            live_hash = live_sig_hash or live_body_hash

            if not live_hash:
                relocated = self._try_relocate_symbol(
                    symbol_name, file_path, recorded_sig_hash, recorded_body_hash, recorded_sketch
                )
                if relocated is not None:
                    item["relocated_from"] = file_path
                    file_path = str(relocated)
                    item["file_path"] = file_path
                    live_sig_hash = compute_ast_signature_hash(file_path, symbol_name)
                    live_body_hash = compute_ast_body_hash(file_path, symbol_name)
                    live_hash = live_sig_hash or live_body_hash

            body_modified = bool(recorded_body_hash and live_body_hash and live_body_hash != recorded_body_hash)
            body_gutted = bool(
                body_modified
                and live_body_hash in _TRIVIAL_STUB_BODIES
                and recorded_body_hash not in _TRIVIAL_STUB_BODIES
            )

            if not live_hash:
                item["status"] = "orphaned_symbol"
                orphaned.append(item)
            elif not body_gutted and not (item_strict and body_modified) and (
                (recorded_sig_hash and live_sig_hash == recorded_sig_hash)
                or (live_body_hash == recorded_hash)
                or (live_sig_hash == recorded_hash)
            ):
                item["status"] = "verified"
                item["last_verified"] = now
                if live_sig_hash:
                    item["signature_hash"] = live_sig_hash
                if live_body_hash:
                    item["body_modified"] = body_modified
                    item["body_hash"] = live_body_hash
                verified.append(item)
            else:
                live_sketch = compute_ast_minhash_sketch(file_path, symbol_name)
                sketch_sim = _minhash_jaccard(recorded_sketch, live_sketch)
                item["status"] = "drifted"
                item["drifted_at"] = now
                item["live_hash"] = live_hash
                if body_gutted:
                    item["drift_reason"] = "body_gutted_to_stub"
                elif item_strict and body_modified:
                    item["drift_reason"] = "strict_body_modified"
                item["structural_similarity"] = round(sketch_sim, 4)
                drifted.append(item)

        if quarantine_drifted and drifted:
            quarantine_file = QUARANTINE_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_quarantine.json"
            with open(quarantine_file, "w", encoding="utf-8") as qf:
                json.dump(drifted, qf, indent=2)

        self.save_grounded_invariants(items)

        return {
            "total_grounded": len(items),
            "verified_count": len(verified),
            "drifted_count": len(drifted),
            "orphaned_count": len(orphaned),
            "verified": verified,
            "drifted": drifted,
            "orphaned": orphaned,
        }
