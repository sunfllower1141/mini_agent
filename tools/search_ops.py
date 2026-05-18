#!/usr/bin/env python3
"""
search_ops.py — semantic search and web search tools for mini_agent.

Tools: find_symbol, find_usages, semantic_search, web_search
"""
from __future__ import annotations

import json
import os
import re as _re
import threading

from safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from tools.shell_ops import _SKIP_DIRS


# ---------------------------------------------------------------------------
# symbol_index — fast workspace symbol lookup
# ---------------------------------------------------------------------------

_SYMBOL_INDEX: dict[str, list[dict]] | None = None  # name → [{"path","line","kind"}, ...]
_INDEX_MAX_MTIME: float = 0.0  # max mtime across all .py files from last build
_INDEX_LAST_PERSIST: float = 0.0  # timestamp of last disk cache write (debounce)


# --- #8 Background indexing ---
import threading
_background_index_thread: threading.Thread | None = None
_background_index_ready: threading.Event = threading.Event()

def _run_background_index(root: str) -> None:
    """Build symbol index in background thread, signal when ready."""
    try:
        build_symbol_index(root)
    finally:
        _background_index_ready.set()

def start_background_index(root: str) -> None:
    """Start background symbol indexing. Non-blocking."""
    global _background_index_thread, _background_index_ready
    _background_index_ready.clear()
    _background_index_thread = threading.Thread(
        target=_run_background_index, args=(root,),
        daemon=True, name="symbol-indexer"
    )
    _background_index_thread.start()

def wait_background_index(timeout: float = 30.0) -> bool:
    """Wait for background index to complete. Returns True if ready."""
    return _background_index_ready.wait(timeout=timeout)


def build_symbol_index(root: str) -> dict[str, list[dict]]:
    """Scan workspace .py files for def/class lines.  Fast — no parsing, just regex.

    Also builds the reference index (_REF_INDEX) in the same pass — no
    second file walk needed.  Both indices are cached in memory.

    Returns {name: [{"path":..., "line":..., "kind":"def"|"class"}, ...]}.
    The index is cached and reused until rebuild_symbol_index is called.
    """
    global _SYMBOL_INDEX, _REF_INDEX, _INDEX_MAX_MTIME
    import re
    import json as _json

    # --- disk cache: avoid re-scanning on every session ---
    cache_path = os.path.join(root, ".mini_agent_index.json")
    cache_mtime = 0.0
    try:
        if os.path.exists(cache_path):
            cache_mtime = os.path.getmtime(cache_path)
    except Exception:
        pass

    # Fast path: if cache exists and we know no .py file is newer, return cached data
    if cache_mtime > 0.0 and _INDEX_MAX_MTIME > 0.0 and cache_mtime >= _INDEX_MAX_MTIME:
        try:
            cached = _json.loads(open(cache_path).read())
            sym = {k: v for k, v in cached.get("symbols", {}).items()}
            ref = {k: v for k, v in cached.get("references", {}).items()}
            _SYMBOL_INDEX = sym
            _REF_INDEX = ref
            return sym
        except Exception:
            pass  # fall through to full rebuild

    def_pat = re.compile(r"^\s*(def|class)\s+(\w+)")
    word_pat = re.compile(r"\b(\w+)\b")

    # Names we never track as references (builtins, common patterns, etc.)
    _SKIP_REF_NAMES = frozenset({
        "self", "cls", "True", "False", "None", "int", "str", "list", "dict",
        "set", "tuple", "bool", "float", "bytes", "type", "object", "super",
        "range", "len", "print", "isinstance", "hasattr", "getattr", "setattr",
        "enumerate", "zip", "map", "filter", "iter", "next", "any", "all",
        "sorted", "reversed", "min", "max", "sum", "abs", "round", "ord", "chr",
        "open", "Exception", "ValueError", "TypeError", "KeyError", "OSError",
        "RuntimeError", "ImportError", "AttributeError", "StopIteration",
        "__init__", "__name__", "__main__", "__file__", "__doc__",
        "unittest", "TestCase", "json", "os", "sys", "re", "time",
    })

    symbol_idx: dict[str, list[dict]] = {}
    # Raw word references collected in a single pass (word, path, line, context).
    # Filtered to known symbol names after the walk completes.
    _raw_refs: list[tuple[str, str, int, str]] = []
    new_max_mtime = 0.0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(fpath)
                if mtime > new_max_mtime:
                    new_max_mtime = mtime
            except OSError:
                pass
            try:
                with open(fpath, "r") as f:
                    for lineno, line in enumerate(f, 1):
                        # Collect def/class definitions
                        m = def_pat.match(line)
                        if m:
                            kind, name = m.group(1), m.group(2)
                            symbol_idx.setdefault(name, []).append({
                                "path": fpath,
                                "line": lineno,
                                "kind": kind,
                            })
                        # Collect all word occurrences for reference index
                        stripped = line.strip()
                        for match in word_pat.finditer(line):
                            word = match.group(1)
                            if word not in _SKIP_REF_NAMES:
                                _raw_refs.append((word, fpath, lineno, stripped[:120]))
            except (OSError, PermissionError):
                continue

    # If cache was valid and no file was newer, return cached data — already handled
    # by the fast path at the top.  Fall through to use the freshly built index.

    # Track max mtime so next call can short-circuit the walk
    _INDEX_MAX_MTIME = new_max_mtime

    # Filter raw references to only known symbol names
    known_names = set(symbol_idx.keys())
    ref_idx: dict[str, list[dict]] = {}
    for word, fpath, lineno, context in _raw_refs:
        if word in known_names:
            ref_idx.setdefault(word, []).append({
                "path": fpath,
                "line": lineno,
                "context": context,
            })

    # Deduplicate references per file+line
    for name in ref_idx:
        seen = set()
        unique = []
        for ref in ref_idx[name]:
            key = (ref["path"], ref["line"])
            if key not in seen:
                seen.add(key)
                unique.append(ref)
        ref_idx[name] = unique

    _SYMBOL_INDEX = symbol_idx
    _REF_INDEX = ref_idx

    # Persist to disk cache
    try:
        cache_path = os.path.join(root, ".mini_agent_index.json")
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as f:
            _json.dump({"symbols": symbol_idx, "references": ref_idx}, f)
        os.replace(tmp, cache_path)  # atomic rename
    except Exception:
        pass

    return symbol_idx


def _reindex_file(filepath: str, root: str) -> None:
    """Re-index a single .py file into the global symbol and reference indices.

    Call this after writing a new/updated .py file so find_symbol and
    find_usages stay current without a full workspace re-scan.
    """
    global _SYMBOL_INDEX, _REF_INDEX
    if _SYMBOL_INDEX is None:
        return  # index not yet built; next find_symbol will build from scratch

    # Add/update symbol definitions
    def_pat = _re.compile(r"^\s*(def|class)\s+(\w+)")
    new_symbols: dict[str, list[dict]] = {}
    try:
        with open(filepath, "r") as f:
            for lineno, line in enumerate(f, 1):
                m = def_pat.match(line)
                if m:
                    kind, name = m.group(1), m.group(2)
                    new_symbols.setdefault(name, []).append({
                        "path": filepath, "line": lineno, "kind": kind,
                    })
    except (OSError, PermissionError):
        return

    # Remove old entries for this file from both indices
    for name in list(_SYMBOL_INDEX.keys()):
        _SYMBOL_INDEX[name] = [e for e in _SYMBOL_INDEX[name] if e["path"] != filepath]
        if not _SYMBOL_INDEX[name]:
            del _SYMBOL_INDEX[name]

    if _REF_INDEX:
        for name in list(_REF_INDEX.keys()):
            _REF_INDEX[name] = [r for r in _REF_INDEX[name] if r["path"] != filepath]
            if not _REF_INDEX[name]:
                del _REF_INDEX[name]

    # Insert new symbol entries
    for name, entries in new_symbols.items():
        _SYMBOL_INDEX.setdefault(name, []).extend(entries)

    # Rebuild reference entries for this file
    if _REF_INDEX is not None:
        word_pat = _re.compile(r"\b(\w+)\b")
        try:
            with open(filepath, "r") as f:
                for lineno, line in enumerate(f, 1):
                    stripped = line.strip()
                    for match in word_pat.finditer(line):
                        word = match.group(1)
                        if word in _SYMBOL_INDEX:
                            _REF_INDEX.setdefault(word, []).append({
                                "path": filepath, "line": lineno, "context": stripped[:120],
                            })
        except (OSError, PermissionError):
            pass

    # Persist updated indices to disk cache so next session picks them up.
    # Debounced: at most one disk write per 2 seconds, regardless of how
    # many .py files are written in rapid succession.
    global _INDEX_LAST_PERSIST
    _DEBOUNCE_S = 2.0
    import time as _time
    now = _time.time()
    if now - _INDEX_LAST_PERSIST < _DEBOUNCE_S:
        return
    _INDEX_LAST_PERSIST = now
    try:
        cache_path = os.path.join(root, ".mini_agent_index.json")
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"symbols": _SYMBOL_INDEX, "references": _REF_INDEX or {}}, f)
        os.replace(tmp, cache_path)  # atomic rename
    except Exception:
        pass


def _get_symbol_index(root: str) -> dict[str, list[dict]]:
    """Return the symbol index, building it lazily if needed."""
    global _SYMBOL_INDEX
    if _SYMBOL_INDEX is None:
        return build_symbol_index(root)
    return _SYMBOL_INDEX


@_register("find_symbol")
def _find_symbol(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Find where a Python symbol (function, class, method) is defined in the workspace."""
    name = args.get("name", "")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    root = rg.workspace_root
    idx = _get_symbol_index(root)

    # Exact match first, then substring
    if name in idx:
        matches = [(name, entries) for name, entries in [(name, idx[name])]]
    else:
        # Substring search — case-insensitive
        matches = []
        pattern = _re.compile(_re.escape(name), _re.IGNORECASE)
        for key, entries in idx.items():
            if pattern.search(key):
                matches.append((key, entries))

    if not matches:
        return ToolResult(
            success=True,
            content=f"No symbols matching '{name}' found in workspace.",
        )

    lines: list[str] = []
    for sym_name, entries in matches[:20]:
        for e in entries[:5]:
            lines.append(f"  {e['kind']:5s}  {sym_name}  →  {e['path']}:{e['line']}")

    prefix = f"Found {sum(len(entries) for _, entries in matches)} location(s) for '{name}':"
    return ToolResult(success=True, content=prefix + "\n" + "\n".join(lines))


@_summarize("find_symbol")
def _find_symbol_summary(args: dict) -> str:
    return f"find_symbol({args.get('name', '?')})"


# ---------------------------------------------------------------------------
# semantic_search (sentence-transformers, local)
# ---------------------------------------------------------------------------

_SEMANTIC_STORE: dict[str, tuple[float, list[tuple[int, int, str, "numpy.ndarray"]]]] = {}
_SEMANTIC_LRU: list[str] = []  # tracks access order for eviction
_SEMANTIC_MAX_ENTRIES = 500    # per-file entries before eviction kicks in
_SEMANTIC_MAX_MTIME: float = 0.0  # max mtime across all indexed files (separate from store)
_SEM_MODEL = None
_SEM_PRELOAD_EVENT = None  # threading.Event: set when model is ready
_SEM_PRELOAD_THREAD = None  # daemon thread reference
_SEM_PRELOAD_LOCK = threading.Lock()  # guards preload state


def _sem_preload() -> None:
    """Start loading the embedding model in a background thread.

    Call this at session startup so the model is ready (or nearly ready)
    by the time anyone calls semantic_search.  Safe to call multiple times
    — subsequent calls are no-ops if the model is already loading or loaded.

    The preload is non-blocking: _sem_get_model() will still block only if
    the model hasn't finished loading yet.  But with typical app startup
    (user typing first query, LLM thinking), the 8s load time hides
    completely behind the initial turn.
    """
    global _SEM_PRELOAD_EVENT, _SEM_PRELOAD_THREAD, _SEM_MODEL
    with _SEM_PRELOAD_LOCK:
        if _SEM_MODEL is not None:
            return  # already loaded
        if _SEM_PRELOAD_EVENT is not None:
            return  # already preloading
        _SEM_PRELOAD_EVENT = threading.Event()

    def _loader() -> None:
        global _SEM_MODEL, _SEM_PRELOAD_EVENT
        try:
            from sentence_transformers import SentenceTransformer
            _SEM_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            pass  # model load failed — _sem_get_model() will retry on demand
        finally:
            _SEM_PRELOAD_EVENT.set()

    _SEM_PRELOAD_THREAD = threading.Thread(target=_loader, daemon=True)
    _SEM_PRELOAD_THREAD.start()


_SEM_MODEL_TIMEOUT = 120  # max seconds to wait for model load before failing


def _sem_get_model():
    """Return the SentenceTransformer model, loading it if needed.

    Never hangs indefinitely: if the preload thread or synchronous load
    takes more than _SEM_MODEL_TIMEOUT seconds, raises TimeoutError so the
    tool returns a clean error instead of deadlocking the agent.
    """
    global _SEM_MODEL, _SEM_PRELOAD_EVENT
    if _SEM_MODEL is not None:
        return _SEM_MODEL
    # If a background preload is in progress, wait for it (with timeout)
    if _SEM_PRELOAD_EVENT is not None:
        import sys
        print('  ⏳ Embedding model still loading (preloaded at startup)...',
              file=sys.stderr, flush=True)
        if not _SEM_PRELOAD_EVENT.wait(timeout=_SEM_MODEL_TIMEOUT):
            raise TimeoutError(
                f"Embedding model preload timed out after {_SEM_MODEL_TIMEOUT}s. "
                "Check network connectivity and retry, or avoid semantic_search."
            )
        if _SEM_MODEL is not None:
            return _SEM_MODEL
        # Preload finished but model is None — load failed silently.
        # Fall through to synchronous load below.
    # Fallback: synchronous load (preload was never called or failed)
    import sys
    print('  ⏳ Loading embedding model (first use, ~9s)...',
          file=sys.stderr, end='', flush=True)
    try:
        from sentence_transformers import SentenceTransformer
        _SEM_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        raise TimeoutError(
            f"Failed to load embedding model: {e}. "
            "Check network connectivity and retry, or avoid semantic_search."
        ) from e
    print(' done.', file=sys.stderr)
    return _SEM_MODEL


def _sem_chunk_py(filepath: str) -> list[tuple[int, int, str]]:
    """Chunk a .py file at def/class boundaries. Returns (start_line, end_line, text)."""
    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
    except (OSError, PermissionError):
        return []

    boundaries = [i for i, ln in enumerate(lines) if ln.strip().startswith(("def ", "class "))]
    if not boundaries:
        text = "".join(lines).strip()
        return [(1, len(lines), text)] if text else []

    chunks: list[tuple[int, int, str]] = []
    for j, start in enumerate(boundaries):
        end = boundaries[j + 1] if j + 1 < len(boundaries) else len(lines)
        text = "".join(lines[start:end]).strip()
        if text:
            chunks.append((start + 1, end, text))
    return chunks


def _merge_symbol_data(
    new_symbols: dict[str, list[dict]],
    raw_refs: list[tuple[str, str, int, str]],
    reindexed_files: set[str],
) -> None:
    """Merge symbol/reference data collected during _sem_index into global indices."""
    global _SYMBOL_INDEX, _REF_INDEX

    # Remove old entries for reindexed files from both indices
    if _SYMBOL_INDEX is not None:
        for name in list(_SYMBOL_INDEX.keys()):
            _SYMBOL_INDEX[name] = [
                e for e in _SYMBOL_INDEX[name]
                if e["path"] not in reindexed_files
            ]
            if not _SYMBOL_INDEX[name]:
                del _SYMBOL_INDEX[name]
    else:
        _SYMBOL_INDEX = {}

    if _REF_INDEX is not None:
        for name in list(_REF_INDEX.keys()):
            _REF_INDEX[name] = [
                r for r in _REF_INDEX[name]
                if r["path"] not in reindexed_files
            ]
            if not _REF_INDEX[name]:
                del _REF_INDEX[name]
    else:
        _REF_INDEX = {}

    # Insert new symbol entries
    for name, entries in new_symbols.items():
        _SYMBOL_INDEX.setdefault(name, []).extend(entries)

    # Filter raw references to only known symbol names
    known_names = set(_SYMBOL_INDEX.keys())
    for word, fpath, lineno, context in raw_refs:
        if word in known_names:
            _REF_INDEX.setdefault(word, []).append({
                "path": fpath, "line": lineno, "context": context,
            })

    # Deduplicate references per file+line
    for name in _REF_INDEX:
        seen = set()
        unique = []
        for ref in _REF_INDEX[name]:
            key = (ref["path"], ref["line"])
            if key not in seen:
                seen.add(key)
                unique.append(ref)
        _REF_INDEX[name] = unique


def _sem_index(root: str) -> None:
    """Build/update in-memory index of .py files.

    Single os.walk pass: checks mtimes, indexes changed files for semantic
    search, AND populates the symbol + reference indices \u2014 no separate
    walk needed.  Returns immediately if no .py file mtimes have changed
    since the last build (fast no-op on repeated calls).
    """
    import numpy as np

    global _SEMANTIC_MAX_MTIME, _SYMBOL_INDEX, _REF_INDEX, _SEMANTIC_LRU

    # --- Regex patterns for symbol/reference indexing (same as build_symbol_index) ---
    import re as _sem_re
    def_pat = _sem_re.compile(r"^\s*(def|class)\s+(\w+)")
    word_pat = _sem_re.compile(r"\b(\w+)\b")
    _SKIP_REF_NAMES = frozenset({
        "self", "cls", "True", "False", "None", "int", "str", "list", "dict",
        "set", "tuple", "bool", "float", "bytes", "type", "object", "super",
        "range", "len", "print", "isinstance", "hasattr", "getattr", "setattr",
        "enumerate", "zip", "map", "filter", "iter", "next", "any", "all",
        "sorted", "reversed", "min", "max", "sum", "abs", "round", "ord", "chr",
        "open", "Exception", "ValueError", "TypeError", "KeyError", "OSError",
        "RuntimeError", "ImportError", "AttributeError", "StopIteration",
        "__init__", "__name__", "__main__", "__file__", "__doc__",
        "unittest", "TestCase", "json", "os", "sys", "re", "time",
    })

    old_max = _SEMANTIC_MAX_MTIME
    new_max = 0.0
    current: set[str] = set()
    any_change = False

    # Accumulators for symbol/reference index building during this walk
    _symbol_entries: dict[str, list[dict]] = {}
    _raw_refs: list[tuple[str, str, int, str]] = []
    _reindexed_files: set[str] = set()  # which files were (re-)read this pass

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            current.add(fpath)
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            if mtime > new_max:
                new_max = mtime
            # Skip if up-to-date
            if fpath in _SEMANTIC_STORE and _SEMANTIC_STORE[fpath][0] == mtime:
                continue
            any_change = True

            # --- Read file once, use for both semantic and symbol/reference indexing ---
            try:
                with open(fpath, "r") as f:
                    file_lines = f.readlines()
            except (OSError, PermissionError):
                continue

            _reindexed_files.add(fpath)

            # --- Symbol/reference scanning (cheap regex, same pass) ---
            for lineno, line in enumerate(file_lines, 1):
                m = def_pat.match(line)
                if m:
                    kind, name = m.group(1), m.group(2)
                    _symbol_entries.setdefault(name, []).append({
                        "path": fpath, "line": lineno, "kind": kind,
                    })
                stripped = line.strip()
                for match in word_pat.finditer(line):
                    word = match.group(1)
                    if word not in _SKIP_REF_NAMES:
                        _raw_refs.append((word, fpath, lineno, stripped[:120]))

            # --- Semantic chunking + encoding (already reads file_lines) ---
            boundaries = [i for i, ln in enumerate(file_lines)
                          if ln.strip().startswith(("def ", "class "))]
            if not boundaries:
                text = "".join(file_lines).strip()
                chunks = [(1, len(file_lines), text)] if text else []
            else:
                chunks = []
                for j, start in enumerate(boundaries):
                    end = boundaries[j + 1] if j + 1 < len(boundaries) else len(file_lines)
                    text = "".join(file_lines[start:end]).strip()
                    if text:
                        chunks.append((start + 1, end, text))

            if not chunks:
                _SEMANTIC_STORE[fpath] = (mtime, [])
                continue
            texts = [t for _, _, t in chunks]
            try:
                model = _sem_get_model()
                embeddings = model.encode(texts, show_progress_bar=False)
            except (TimeoutError, OSError) as e:
                import sys
                print(f"  ⚠ semantic index: model load failed ({e}), "
                      "skipping semantic encoding. Symbol/reference index still built.",
                      file=sys.stderr, flush=True)
                _SEMANTIC_STORE[fpath] = (mtime, [])
                continue
            # Pre-normalize embeddings for fast cosine via dot product later
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-9)
            _SEMANTIC_STORE[fpath] = (mtime, list(zip(
                [s for s, e, _ in chunks],
                [e for s, e, _ in chunks],
                texts,
                list(embeddings),
            )))

            # LRU eviction: if we exceed the cap, drop the oldest entry
            if fpath not in _SEMANTIC_LRU:
                _SEMANTIC_LRU.append(fpath)
            while len(_SEMANTIC_LRU) > _SEMANTIC_MAX_ENTRIES:
                old = _SEMANTIC_LRU.pop(0)
                if old in _SEMANTIC_STORE:
                    del _SEMANTIC_STORE[old]

    # Clean stale entries (always, even on no-change short circuit)
    stale = [p for p in _SEMANTIC_STORE if p not in current]
    for p in stale:
        del _SEMANTIC_STORE[p]
    _SEMANTIC_MAX_MTIME = new_max

    # --- Merge collected symbol/reference data into global indices ---
    if any_change or _SYMBOL_INDEX is None:
        if _symbol_entries or _reindexed_files:
            _merge_symbol_data(_symbol_entries, _raw_refs, _reindexed_files)

    # If nothing changed at all, bail out early
    if not any_change and old_max > 0:
        return


@_register("semantic_search")
def _semantic_search(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    query = args.get("query", "")
    if not query:
        return ToolResult(success=False, content="Missing required parameter: 'query'.")
    path = args.get("path", ".")
    safety_result = rg.check(path)
    if not safety_result.allowed:
        return ToolResult(success=False, content=f"Search blocked by safety layer: {safety_result.reason}")

    import numpy as np

    root = safety_result.resolved_path
    _sem_index(root)

    try:
        model = _sem_get_model()
        query_emb = model.encode([query], show_progress_bar=False)[0]
    except (TimeoutError, OSError) as e:
        return ToolResult(
            success=False,
            content=f"Semantic search unavailable: {e}. "
                    "Check network connectivity and retry, or use search_files / find_symbol instead.",
        )
    # Normalize query embedding for cosine via dot product
    query_emb = query_emb / (np.linalg.norm(query_emb) + 1e-9)

    # Collect all chunk embeddings (already normalized in _sem_index) and metadata
    metas: list[tuple[str, int, int, str]] = []
    embs: list[np.ndarray] = []
    for fpath, value in _SEMANTIC_STORE.items():
        _, chunks = value
        for start, end, text, emb in chunks:
            metas.append((fpath, start, end, text))
            embs.append(emb)

    if not embs:
        return ToolResult(success=True, content="No matches found.")

    # Touch LRU: files accessed during search should be moved to end (most-recently-used)
    for fpath in _SEMANTIC_STORE:
        if fpath in _SEMANTIC_LRU:
            _SEMANTIC_LRU.remove(fpath)
            _SEMANTIC_LRU.append(fpath)

    # Batched matmul: all cosine similarities in one call
    emb_matrix = np.asarray(embs)  # shape (N, D)
    scores = np.dot(emb_matrix, query_emb)  # shape (N,)
    top_indices = np.argsort(scores)[-10:][::-1]  # top 10, descending

    top: list[tuple[float, str, int, int, str]] = []
    for idx in top_indices:
        fpath, start, end, text = metas[idx]
        top.append((float(scores[idx]), fpath, start, end, text))

    if not top:
        return ToolResult(success=True, content="No matches found.")

    lines: list[str] = []
    for cos, fpath, start, end, text in top:
        lines.append(f"score={cos:.3f}  {fpath}:{start}-{end}")
        snippet = text[:200].replace("\n", "\\n")
        if len(text) > 200:
            snippet += "…"
        lines.append(f"  {snippet}")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("semantic_search")
def _semantic_search_summary(args: dict) -> str:
    query = args.get("query", "?")
    preview = query[:60]
    if len(query) > 60:
        preview += "…"
    return f"semantic_search({preview})"


# ---------------------------------------------------------------------------
# web_search (Exa)
# ---------------------------------------------------------------------------

@_register("web_search")
def _web_search(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    query = args.get("query", "")
    if not query:
        return ToolResult(success=False, content="Missing required parameter: 'query'.")
    num = min(args.get("num_results", 5), 20)
    stype = args.get("search_type", "auto")
    api_key = _TOOL_CONTEXT.exa_api_key or os.environ.get("EXA_API_KEY", "")

    if not api_key:
        # Fall back to DuckDuckGo (free, no API key needed)
        return _web_search_ddg(query, num)

    try:
        from exa_py import Exa
        exa = Exa(api_key=api_key)
        response = exa.search(
            query,
            type=stype,
            num_results=num,
            contents={"highlights": True},
        )
    except Exception as e:
        return ToolResult(success=False, content=f"Exa search error: {e}")

    if not response.results:
        return ToolResult(success=True, content="No results found.")

    lines: list[str] = []
    for i, r in enumerate(response.results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {r.url}")
        if r.highlights:
            for h in r.highlights[:3]:
                lines.append(f"   > {h.strip()}")
        lines.append("")

    return ToolResult(success=True, content="\n".join(lines).rstrip())


def _web_search_ddg(query: str, num: int = 5) -> ToolResult:
    """Fallback web search using DuckDuckGo's HTML (no API key)."""
    try:
        from urllib.request import Request, urlopen
        from urllib.parse import quote_plus
        import html as _html
        import re as _re
        import json as _json

        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        req = Request(url, headers={"User-Agent": "mini_agent/1.0"})
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # Extract result blocks: <a class="result__a" href="...">Title</a>
        # and <a class="result__snippet" >Snippet</a>
        results: list[dict] = []
        snippet_pat = _re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', _re.DOTALL
        )
        link_pat = _re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', _re.DOTALL
        )

        links = link_pat.findall(raw)
        snippets = snippet_pat.findall(raw)

        for i, (href, title) in enumerate(links):
            if i >= num:
                break
            title_clean = _re.sub(r"<[^>]*>", "", title).strip()
            title_clean = _html.unescape(title_clean)
            snippet = ""
            if i < len(snippets):
                snippet = _re.sub(r"<[^>]*>", "", snippets[i]).strip()
                snippet = _html.unescape(snippet)
            results.append({
                "title": title_clean,
                "url": href,
                "snippet": snippet,
            })

        if not results:
            return ToolResult(success=True, content="No results found (DuckDuckGo fallback).")

        lines: list[str] = ["(via DuckDuckGo fallback — no Exa key configured)\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   {r['url']}")
            if r["snippet"]:
                lines.append(f"   > {r['snippet']}")
            lines.append("")
        return ToolResult(success=True, content="\n".join(lines).rstrip())
    except Exception as e:
        return ToolResult(success=False, content=f"DuckDuckGo fallback search error: {e}")


@_summarize("web_search")
def _web_search_summary(args: dict) -> str:
    query = args.get("query", "?")
    preview = query[:60]
    if len(query) > 60:
        preview += "…"
    return f"web_search({preview})"


# ---------------------------------------------------------------------------
# find_usages — cross-reference lookup
# ---------------------------------------------------------------------------

# Reverse index: for each symbol name, all lines where it's referenced
# (as a bare word in Python source).  Built lazily.
_REF_INDEX: dict[str, list[dict]] | None = None



def _get_ref_index(root: str) -> dict[str, list[dict]]:
    """Return the reference index, building it lazily.
    
    Delegates to _get_symbol_index which builds both the symbol and
    reference indices in a single workspace walk — no duplicate I/O.
    """
    global _REF_INDEX
    if _REF_INDEX is None:
        _get_symbol_index(root)  # builds both _SYMBOL_INDEX and _REF_INDEX
        if _REF_INDEX is None:
            _REF_INDEX = {}
    return _REF_INDEX


@_register("find_usages")
def _find_usages(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Find all usages (references) of a Python symbol in the workspace."""
    import re
    name = args.get("name", "")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    root = rg.workspace_root
    ref_idx = _get_ref_index(root)

    if not ref_idx:
        return ToolResult(
            success=True,
            content=f"Reference index not yet built. Try find_symbol first to populate the forward index.",
        )

    # Exact match first, then substring
    if name in ref_idx:
        matches = ref_idx[name]
    else:
        # Substring search
        matches = []
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        for key, refs in ref_idx.items():
            if pattern.search(key):
                matches.extend(refs)

    if not matches:
        # Fall back to grep-based search
        import subprocess
        from tools.shell_ops import _SKIP_DIRS
        try:
            cmd = ["grep", "-rn", "--include=*.py"]
            for d in _SKIP_DIRS:
                cmd.extend(["--exclude-dir", d])
            cmd.extend(["-w", name, root])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.stdout.strip():
                lines_out = result.stdout.strip().split("\n")[:30]
                return ToolResult(
                    success=True,
                    content=f"Found usages of '{name}' (grep fallback):\n" + "\n".join(f"  {l}" for l in lines_out),
                )
            if result.stderr.strip():
                import sys
                print(f"Warning: grep stderr for '{name}': {result.stderr.strip()[:200]}", file=sys.stderr)
        except Exception as exc:
            import sys
            print(f"Warning: find_usages grep fallback failed for '{name}': {exc}", file=sys.stderr)
        return ToolResult(
            success=True,
            content=f"No usages found for '{name}' in workspace.",
        )

    # Limit output — context is truncated to 60 chars to keep tool responses
    # lightweight. Full multi-line context makes sub-agents hit 400 errors.
    shown = matches[:30]
    lines: list[str] = [f"Found {len(matches)} usage(s) of '{name}':"]
    for ref in shown:
        ctx = ref.get('context', '')
        ctx = ctx.strip().replace('\n', ' | ')[:60]
        lines.append(f"  {ref['path']}:{ref['line']}  {ctx}")

    if len(matches) > 30:
        lines.append(f"  … and {len(matches) - 30} more")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("find_usages")
def _find_usages_summary(args: dict) -> str:
    return f"find_usages({args.get('name', '?')})"


# ---------------------------------------------------------------------------
# recall_turn — retrieve a summary of a past turn
# ---------------------------------------------------------------------------

@_summarize("recall_turn")
def _recall_turn_summary(args: dict) -> str:
    return f"recall_turn({args.get('turn', '?')})"


# ---------------------------------------------------------------------------
# fetch_url -- fetch a web page and return its content
# ---------------------------------------------------------------------------

@_register("fetch_url")
@_summarize("fetch_url")
def _fetch_url(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Fetch a URL and return its text content (truncated)."""
    import urllib.request
    import urllib.error
    url = args["url"]
    timeout = min(args.get("timeout", 15), 30)
    max_chars = args.get("max_chars", 10000)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "mini_agent/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ToolResult(
                success=False,
                content=f"Cannot read content type: {content_type}. fetch_url only supports text/html and text/plain.",
            )
        data = resp.read().decode("utf-8", errors="replace")
        truncated = data[:max_chars]
        status = f"HTTP {resp.status}, {len(data)} chars"
        if len(data) > max_chars:
            status += f" (showing first {max_chars})"
        return ToolResult(success=True, content=f"[{status}]\n\n{truncated}")
    except urllib.error.URLError as e:
        return ToolResult(success=False, content=f"Failed to fetch URL: {e}")
    except Exception as e:
        return ToolResult(success=False, content=f"Error fetching URL: {e}")


@_summarize("fetch_url")
def _fetch_url_summary(args: dict) -> str:
    url = args.get("url", "?")
    short = url[:50] + "..." if len(url) > 50 else url
    return f"fetch_url({short})"
