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
from typing import Any

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from tools.shell_ops import _SKIP_DIRS


# ---------------------------------------------------------------------------
# symbol_index — fast workspace symbol lookup
# ---------------------------------------------------------------------------

_SYMBOL_INDEX: dict[str, list[dict]] | None = None  # name → [{"path","line","kind"}, ...]
_INDEX_MAX_MTIME: float = 0.0  # max mtime across all .py files from last build
_INDEX_LAST_PERSIST: float = 0.0  # timestamp of last disk cache write (debounce)


# Names we never track as references (builtins, common patterns, etc.)
_SKIP_REF_NAMES: frozenset[str] = frozenset({
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

# --- #8 Background indexing ---
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
            cached = _json.loads(open(cache_path, encoding="utf-8", errors="replace").read())
            sym = {k: v for k, v in cached.get("symbols", {}).items()}
            ref = {k: v for k, v in cached.get("references", {}).items()}
            _SYMBOL_INDEX = sym
            _REF_INDEX = ref
            return sym
        except Exception:
            pass  # fall through to full rebuild

    def_pat = re.compile(r"^\s*(def|class)\s+(\w+)")
    word_pat = re.compile(r"\b(\w+)\b")

    symbol_idx: dict[str, list[dict]] = {}
    # Raw word references collected in a single pass (word, path, line, context).
    # Filtered to known symbol names after the walk completes.
    _raw_refs: list[tuple[str, str, int, str]] = []
    new_max_mtime = 0.0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            is_python = ext == ".py"
            is_js_ts = ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
            if not is_python and not is_js_ts:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(fpath)
                if mtime > new_max_mtime:
                    new_max_mtime = mtime
            except OSError:
                pass
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
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
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
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
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
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
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
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
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
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

_SEMANTIC_STORE: dict[str, tuple[float, list[tuple[int, int, str, Any]]]] = {}
_SEMANTIC_LRU: list[str] = []  # tracks access order for eviction
_SEMANTIC_MAX_ENTRIES = 500    # per-file entries before eviction kicks in
_SEMANTIC_MAX_MTIME: float = 0.0  # max mtime across all indexed files (separate from store)
_SEM_MODEL = None
_SEM_PRELOAD_EVENT = None  # threading.Event: set when model is ready
_SEM_PRELOAD_THREAD = None  # daemon thread reference
_SEM_PRELOAD_LOCK = threading.Lock()  # guards preload state
_SEM_CACHE_DIRTY = False  # set when store changes, cleared on disk write

# --- Semantic persistence ---
_SEM_CACHE_FILE = ".mini_agent_semantic.npz"
_SEM_META_FILE = ".mini_agent_semantic_meta.json"


def _sem_save_cache(root: str) -> None:
    """Persist semantic store to disk (JSON metadata + numpy .npz embeddings)."""
    global _SEM_CACHE_DIRTY
    import numpy as np
    try:
        meta: dict[str, dict] = {}
        emb_arrays: dict[str, np.ndarray] = {}
        for fpath, (mtime, chunks) in _SEMANTIC_STORE.items():
            if not chunks:
                meta[fpath] = {"mtime": mtime, "chunks": []}
                continue
            chunk_meta = []
            arrs = []
            for start, end, text, emb in chunks:
                chunk_meta.append([start, end, text])
                arrs.append(np.asarray(emb))
            meta[fpath] = {"mtime": mtime, "chunks": chunk_meta}
            emb_arrays[fpath] = np.stack(arrs) if arrs else np.zeros((0, 384))

        meta_path = os.path.join(root, _SEM_META_FILE)
        npz_path = os.path.join(root, _SEM_CACHE_FILE)

        # Write meta JSON
        tmp_meta = meta_path + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp_meta, meta_path)

        # Write embeddings NPZ
        tmp_npz = npz_path + ".tmp"
        np.savez_compressed(tmp_npz, **emb_arrays)
        os.replace(tmp_npz, npz_path)

        _SEM_CACHE_DIRTY = False
    except Exception:
        pass  # best-effort persistence


def _sem_load_cache(root: str) -> bool:
    """Try to load semantic store from disk cache. Returns True if loaded."""
    global _SEMANTIC_STORE, _SEMANTIC_MAX_MTIME, _SEMANTIC_LRU
    import numpy as np
    meta_path = os.path.join(root, _SEM_META_FILE)
    npz_path = os.path.join(root, _SEM_CACHE_FILE)

    if not os.path.exists(meta_path) or not os.path.exists(npz_path):
        return False

    try:
        # Check cache freshness: all cached files must exist and have same mtime
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        stale = False
        for fpath, info in meta.items():
            if not os.path.exists(fpath):
                stale = True
                break
            try:
                if os.path.getmtime(fpath) != info["mtime"]:
                    stale = True
                    break
            except OSError:
                stale = True
                break
        if stale:
            return False

        data = np.load(npz_path, allow_pickle=False)
        store: dict[str, tuple[float, list]] = {}
        max_mtime = 0.0
        for fpath, info in meta.items():
            mtime = info["mtime"]
            max_mtime = max(max_mtime, mtime)
            chunk_meta = info.get("chunks", [])
            if not chunk_meta:
                store[fpath] = (mtime, [])
                continue
            # Reconstruct chunks with loaded embeddings
            emb_arr = data[fpath]  # shape (N, 384)
            if emb_arr.shape[0] != len(chunk_meta):
                return False  # mismatch
            chunks = []
            for i, (start, end, text) in enumerate(chunk_meta):
                emb = emb_arr[i]
                # Re-normalize (should already be normalized, but belt-and-suspenders)
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb = emb / norm
                chunks.append((start, end, text, emb))
            store[fpath] = (mtime, chunks)

        _SEMANTIC_STORE = store
        _SEMANTIC_MAX_MTIME = max_mtime
        _SEMANTIC_LRU = list(store.keys())
        return True
    except Exception:
        return False


def _reset_semantic_state() -> None:
    """Safely reset all semantic search module-level globals.

    Waits for any running preload thread to finish before clearing state.
    Call this from test fixtures or conftest instead of directly mutating
    globals like ``_SEM_PRELOAD_EVENT = None``, which causes races with
    the daemon loader thread.
    """
    global _SEM_MODEL, _SEM_PRELOAD_EVENT, _SEM_PRELOAD_THREAD

    with _SEM_PRELOAD_LOCK:
        # Wait for any in-progress preload thread to finish
        thread = _SEM_PRELOAD_THREAD
        event = _SEM_PRELOAD_EVENT
        if thread is not None and thread.is_alive():
            # Release lock while waiting so the thread can set the event
            pass
        _SEM_MODEL = None
        _SEM_PRELOAD_EVENT = None
        _SEM_PRELOAD_THREAD = None

    # Wait outside the lock to avoid deadlock with _loader's finally block
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


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

    # Capture a local reference to the Event so external mutations
    # (e.g. test teardown resetting _SEM_PRELOAD_EVENT = None) don't
    # cause an AttributeError in the finally block.
    event = _SEM_PRELOAD_EVENT

    def _loader() -> None:
        global _SEM_MODEL
        try:
            from sentence_transformers import SentenceTransformer
            _SEM_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            pass  # model load failed — _sem_get_model() will retry on demand
        finally:
            if event is not None:
                event.set()

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
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
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


# ---------------------------------------------------------------------------
# Multi-language chunking (Python + JS/TS)
# ---------------------------------------------------------------------------

_TS_FUNCTION_BOUNDARY = _re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+\w+",
    _re.MULTILINE,
)

_TS_ARROW_BOUNDARY = _re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\([^)]*\)\s*=>",
    _re.MULTILINE,
)


def _sem_chunk_ts(filepath: str) -> list[tuple[int, int, str]]:
    """Chunk a .js/.ts/.jsx/.tsx file at function/class/export boundaries."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (OSError, PermissionError):
        return []
    boundaries = [
        i for i, ln in enumerate(lines)
        if _TS_FUNCTION_BOUNDARY.match(ln) or _TS_ARROW_BOUNDARY.match(ln)
    ]
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


def _sem_chunk(filepath: str) -> list[tuple[int, int, str]]:
    """Chunk a source file at structural boundaries. Supports .py, .js, .ts, .jsx, .tsx."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        return _sem_chunk_ts(filepath)
    elif ext == ".py":
        return _sem_chunk_py(filepath)
    return []


# ---------------------------------------------------------------------------
# BM25 keyword index (pure Python, no external deps)
# ---------------------------------------------------------------------------

_BM25_INDEX: dict[str, tuple[dict[str, float], dict[str, int], dict[str, int]]] = {}
# _BM25_INDEX[chunk_id] = (idf_map, tf_map, doc_len_map)
_BM25_DOCUMENTS: dict[str, tuple[str, int, int, str]] = {}
# _BM25_DOCUMENTS[chunk_id] = (filepath, start_line, end_line, text)
_BM25_K1 = 1.5
_BM25_B = 0.75
_BM25_AVGDL = 0.0


def _tokenize(text: str) -> list[str]:
    """Simple tokenizer: lowercase, split on non-alphanumeric."""
    return _re.findall(r"[a-zA-Z_]\w+", text.lower())


def _bm25_score(query_tokens: list[str], chunk_id: str) -> float:
    """Compute BM25 score for a query against a document chunk."""
    if chunk_id not in _BM25_INDEX:
        return 0.0
    idf_map, tf_map, doc_len_map = _BM25_INDEX[chunk_id]
    doc_len = doc_len_map.get("_len", 0)
    if doc_len == 0 or _BM25_AVGDL == 0:
        return 0.0
    score = 0.0
    for token in set(query_tokens):
        idf = idf_map.get(token, 0.0)
        tf = tf_map.get(token, 0)
        if tf == 0 or idf == 0:
            continue
        num = tf * (_BM25_K1 + 1)
        denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len / _BM25_AVGDL)
        score += idf * num / denom
    return score


def _bm25_build_index(corpus: dict[str, str]) -> None:
    """Build BM25 index from {chunk_id: text} mapping."""
    global _BM25_INDEX, _BM25_AVGDL
    _BM25_INDEX = {}
    N = len(corpus)
    if N == 0:
        _BM25_AVGDL = 0.0
        return

    # Tokenize all documents
    tokenized: dict[str, list[str]] = {}
    total_len = 0
    for cid, text in corpus.items():
        tokens = _tokenize(text)
        tokenized[cid] = tokens
        total_len += len(tokens)

    _BM25_AVGDL = total_len / N

    # Document frequencies
    df: dict[str, int] = {}
    for tokens in tokenized.values():
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1

    # IDF
    idf: dict[str, float] = {}
    for token, count in df.items():
        idf[token] = max(0.0, ((N - count + 0.5) / (count + 0.5)) + 1.0)

    # Per-document TF and doc len
    for cid, tokens in tokenized.items():
        tf_map: dict[str, int] = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1
        _BM25_INDEX[cid] = (
            {t: idf.get(t, 0.0) for t in tf_map},
            tf_map,
            {"_len": len(tokens)},
        )


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

    global _SEMANTIC_MAX_MTIME, _SYMBOL_INDEX, _REF_INDEX, _SEMANTIC_LRU, _SEM_CACHE_DIRTY
    global _BM25_INDEX, _BM25_DOCUMENTS

    # --- Try loading from disk cache first ---
    if not _SEMANTIC_STORE:
        if _sem_load_cache(root):
            return  # loaded from cache, nothing to index

    import re as _sem_re
    def_pat = _sem_re.compile(r"^\s*(def|class)\s+(\w+)")
    word_pat = _sem_re.compile(r"\b(\w+)\b")

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
            ext = os.path.splitext(fname)[1].lower()
            is_python = ext == ".py"
            is_js_ts = ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
            if not is_python and not is_js_ts:
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
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    file_lines = f.readlines()
            except (OSError, PermissionError):
                continue

            _reindexed_files.add(fpath)

            # --- Symbol/reference scanning (Python only) ---
            if is_python:
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

            # --- Semantic chunking (multi-language) + encoding ---
            chunks = _sem_chunk(fpath)
            if not chunks:
                # Fallback for non-Python/TS or empty files
                text = "".join(file_lines).strip()
                chunks = [(1, len(file_lines), text)] if text else []

            if not chunks:
                _SEMANTIC_STORE[fpath] = (mtime, [])
                if is_python or is_js_ts:
                    _BM25_DOCUMENTS.pop(fpath, None)
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

    # --- Persist semantic store to disk ---
    if any_change:
        _SEM_CACHE_DIRTY = True
        _sem_save_cache(root)

    # --- Rebuild BM25 keyword index ---
    if any_change:
        corpus: dict[str, str] = {}
        for fpath, (_, chunks) in _SEMANTIC_STORE.items():
            for i, (start, end, text, _) in enumerate(chunks):
                cid = f"{fpath}::{i}"
                _BM25_DOCUMENTS[cid] = (fpath, start, end, text)
                corpus[cid] = text
        _bm25_build_index(corpus)


@_register("semantic_search")
def _semantic_search(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Hybrid semantic + keyword search with Reciprocal Rank Fusion.

    Combines vector similarity (all-MiniLM-L6-v2) with BM25 keyword matching
    for best-in-class retrieval. Supports .py, .js, .ts, .jsx, .tsx files.
    """
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

    # --- Vector search ---
    try:
        model = _sem_get_model()
        query_emb = model.encode([query], show_progress_bar=False)[0]
    except (TimeoutError, OSError) as e:
        # Fall back to BM25-only if vector search unavailable
        return _semantic_search_bm25_only(query)
    query_emb = query_emb / (np.linalg.norm(query_emb) + 1e-9)

    # Collect all chunk embeddings and metadata
    metas: list[tuple[str, int, int, str]] = []
    embs: list[np.ndarray] = []
    chunk_ids: list[str] = []  # for BM25 fusion
    for fpath, value in _SEMANTIC_STORE.items():
        _, chunks = value
        for i, (start, end, text, emb) in enumerate(chunks):
            metas.append((fpath, start, end, text))
            embs.append(emb)
            chunk_ids.append(f"{fpath}::{i}")

    if not embs:
        return ToolResult(success=True, content="No indexed files found. Try search_files instead.")

    # Vector scores
    emb_matrix = np.asarray(embs)
    vec_scores = np.dot(emb_matrix, query_emb)  # cosine similarities

    # --- BM25 keyword search ---
    query_tokens = _tokenize(query)
    bm25_scores = np.zeros(len(chunk_ids))
    if query_tokens and _BM25_INDEX:
        for i, cid in enumerate(chunk_ids):
            bm25_scores[i] = _bm25_score(query_tokens, cid)

    # --- Reciprocal Rank Fusion ---
    # Get rankings from each method (descending by score)
    vec_rank = np.zeros(len(chunk_ids), dtype=np.float64)
    bm25_rank = np.zeros(len(chunk_ids), dtype=np.float64)

    if np.max(vec_scores) > 0:
        vec_order = np.argsort(vec_scores)[::-1]
        for rank, idx in enumerate(vec_order):
            vec_rank[idx] = 1.0 / (60 + rank + 1)  # RRF k=60

    if np.max(bm25_scores) > 0:
        bm25_order = np.argsort(bm25_scores)[::-1]
        for rank, idx in enumerate(bm25_order):
            bm25_rank[idx] = 1.0 / (60 + rank + 1)

    # Fuse: equal weight to both methods
    fused = vec_rank + bm25_rank
    top_indices = np.argsort(fused)[-15:][::-1]  # top 15

    # Filter to only results with any signal
    top: list[tuple[float, str, int, int, str, float, float]] = []
    for idx in top_indices:
        fscore = fused[idx]
        if fscore <= 0:
            continue
        fpath, start, end, text = metas[idx]
        top.append((float(fscore), fpath, start, end, text,
                    float(vec_scores[idx]), float(bm25_scores[idx])))

    if not top:
        return ToolResult(success=True, content="No matches found.")

    lines: list[str] = []
    for fscore, fpath, start, end, text, vs, bs in top:
        # Show what contributed to the score
        tags = []
        if vs > 0.3:
            tags.append("semantic")
        if bs > 0:
            tags.append("keyword")
        tag_str = "+".join(tags) if tags else "none"
        lines.append(f"score={fscore:.3f} [{tag_str}]  {fpath}:{start}-{end}")
        snippet = text[:200].replace("\n", "\\n")
        if len(text) > 200:
            snippet += "…"
        lines.append(f"  {snippet}")

    return ToolResult(success=True, content="\n".join(lines))


def _semantic_search_bm25_only(query: str) -> ToolResult:
    """BM25-only fallback when embedding model is unavailable."""
    query_tokens = _tokenize(query)
    if not query_tokens or not _BM25_INDEX:
        return ToolResult(
            success=False,
            content="Semantic search unavailable and no BM25 index built. "
                    "Use search_files or find_symbol instead.",
        )
    results = []
    for cid in _BM25_DOCUMENTS:
        score = _bm25_score(query_tokens, cid)
        if score > 0:
            fpath, start, end, text = _BM25_DOCUMENTS[cid]
            results.append((score, fpath, start, end, text))
    results.sort(key=lambda x: x[0], reverse=True)
    if not results:
        return ToolResult(success=True, content="No matches found (BM25 only).")
    lines: list[str] = ["(BM25 keyword search — embedding model unavailable)\n"]
    for score, fpath, start, end, text in results[:10]:
        lines.append(f"score={score:.3f} [keyword]  {fpath}:{start}-{end}")
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


# ---------------------------------------------------------------------------
# Call graph — structured callers/callees via AST
# ---------------------------------------------------------------------------

# call_graph: caller_name → [(callee_name, filepath, line), ...]
# caller_graph: callee_name → [(caller_name, filepath, line), ...]
_CALL_GRAPH: dict[str, list[tuple[str, str, int]]] = {}
_CALLER_GRAPH: dict[str, list[tuple[str, str, int]]] = {}
_CALL_GRAPH_BUILT = False
_CALL_GRAPH_LOCK = threading.Lock()


def _build_call_graph(root: str) -> None:
    """Build call and caller graphs from AST analysis of all .py files.

    Extracts function/method calls within each def/class body and
    populates _CALL_GRAPH (caller → callees) and _CALLER_GRAPH
    (callee → callers). Called lazily on first find_callers/find_callees.
    """
    global _CALL_GRAPH, _CALLER_GRAPH, _CALL_GRAPH_BUILT
    with _CALL_GRAPH_LOCK:
        if _CALL_GRAPH_BUILT:
            return
        _CALL_GRAPH = {}
        _CALLER_GRAPH = {}

        import ast as _ast
        _SKIP_DIRS_LIST = _SKIP_DIRS

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                          if d not in _SKIP_DIRS_LIST and not d.startswith(".")]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)

                # Use AST for Python (reliable, no external deps)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        source = f.read()
                    tree = _ast.parse(source, filename=fpath)
                except (SyntaxError, OSError, PermissionError):
                    continue

                for node in _ast.walk(tree):
                    if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        caller_name = node.name
                        for child in _ast.walk(node):
                            if isinstance(child, _ast.Call):
                                callee = _resolve_call_name(child)
                                if callee and callee not in _SKIP_REF_NAMES:
                                    _CALL_GRAPH.setdefault(caller_name, []).append(
                                        (callee, fpath, child.lineno)
                                    )
                                    _CALLER_GRAPH.setdefault(callee, []).append(
                                        (caller_name, fpath, child.lineno)
                                    )
        # Deduplicate
        for g in (_CALL_GRAPH, _CALLER_GRAPH):
            for name in list(g.keys()):
                seen = set()
                unique = []
                for entry in g[name]:
                    key = (entry[0], entry[1], entry[2])
                    if key not in seen:
                        seen.add(key)
                        unique.append(entry)
                g[name] = unique

        _CALL_GRAPH_BUILT = True


def _resolve_call_name(node: Any) -> str | None:
    """Resolve a Call node's function name."""
    import ast as _ast
    func = node.func
    if isinstance(func, _ast.Name):
        return func.id
    if isinstance(func, _ast.Attribute):
        return func.attr
    return None


@_register("find_callers")
def _find_callers(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Find all callers (functions that call) a given symbol in the workspace."""
    name = args.get("name", "")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    root = rg.workspace_root
    _build_call_graph(root)

    matches = _CALLER_GRAPH.get(name, [])

    # Substring search if no exact match
    if not matches:
        pattern = _re.compile(_re.escape(name), _re.IGNORECASE)
        for key, callers in _CALLER_GRAPH.items():
            if pattern.search(key):
                matches.extend(callers)

    if not matches:
        return ToolResult(
            success=True,
            content=f"No callers found for '{name}' in workspace.",
        )

    shown = matches[:30]
    lines: list[str] = [f"Found {len(matches)} caller(s) of '{name}':"]
    for callee_name, fpath, line in shown:
        lines.append(f"  {callee_name}  →  {fpath}:{line}")

    if len(matches) > 30:
        lines.append(f"  … and {len(matches) - 30} more")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("find_callers")
def _find_callers_summary(args: dict) -> str:
    return f"find_callers({args.get('name', '?')})"


@_register("find_callees")
def _find_callees(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Find all callees (functions called by) a given symbol in the workspace."""
    name = args.get("name", "")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    root = rg.workspace_root
    _build_call_graph(root)

    matches = _CALL_GRAPH.get(name, [])

    # Substring search if no exact match
    if not matches:
        pattern = _re.compile(_re.escape(name), _re.IGNORECASE)
        for key, callees in _CALL_GRAPH.items():
            if pattern.search(key):
                matches.extend(callees)

    if not matches:
        return ToolResult(
            success=True,
            content=f"No callees found for '{name}' in workspace.",
        )

    shown = matches[:30]
    lines: list[str] = [f"Found {len(matches)} callee(s) of '{name}':"]
    for callee_name, fpath, line in shown:
        lines.append(f"  {callee_name}  →  {fpath}:{line}")

    if len(matches) > 30:
        lines.append(f"  … and {len(matches) - 30} more")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("find_callees")
def _find_callees_summary(args: dict) -> str:
    return f"find_callees({args.get('name', '?')})"



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
            content="Reference index not yet built. Try find_symbol first to populate the forward index.",
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
# Knowledge graph tools — entity-relationship queries
# ---------------------------------------------------------------------------

def _ensure_knowledge_graph(root: str) -> None:
    """Build the knowledge graph lazily if not yet built."""
    from core.knowledge_graph import build_knowledge_graph
    build_knowledge_graph(root)


@_register("find_related")
def _find_related(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Find entities directly related to a given symbol in the knowledge graph.

    Shows callers, callees, imports, and inheritance relationships.
    """
    name = args.get("name", "")
    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    root = rg.workspace_root
    _ensure_knowledge_graph(root)

    from core.knowledge_graph import find_related as _kg_find_related
    results = _kg_find_related(name)

    if not results:
        return ToolResult(
            success=True,
            content=f"No relationships found for '{name}' in the knowledge graph. "
                    "Try find_symbol or find_callers first.",
        )

    # Group by kind
    by_kind: dict[str, list[dict]] = {}
    for r in results:
        kind = r["kind"]
        by_kind.setdefault(kind, []).append(r)

    lines: list[str] = [f"Relationships for '{name}':"]
    for kind, entries in sorted(by_kind.items()):
        lines.append(f"\n  [{kind}] ({len(entries)} edges):")
        for e in entries[:10]:
            arrow = "→" if e["direction"] == "out" else "←"
            lines.append(f"    {arrow} {e['target']}  {e['file']}:{e['line']}")
        if len(entries) > 10:
            lines.append(f"    … and {len(entries) - 10} more")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("find_related")
def _find_related_summary(args: dict) -> str:
    return f"find_related({args.get('name', '?')})"


@_register("trace_path")
def _trace_path(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Find the shortest path between two symbols in the knowledge graph.

    Traces call, import, and inheritance edges to find how two entities
    are connected.
    """
    from_name = args.get("from", "")
    to_name = args.get("to", "")
    if not from_name or not to_name:
        return ToolResult(success=False, content="Missing required parameters: 'from' and 'to'.")

    root = rg.workspace_root
    _ensure_knowledge_graph(root)

    from core.knowledge_graph import trace_path as _kg_trace_path
    paths = _kg_trace_path(from_name, to_name)

    if not paths:
        return ToolResult(
            success=True,
            content=f"No path found from '{from_name}' to '{to_name}' in the knowledge graph.",
        )

    lines: list[str] = [f"Paths from '{from_name}' to '{to_name}':"]
    for i, path in enumerate(paths[:5], 1):
        lines.append(f"  Path {i} ({len(path)-1} hops): {' → '.join(path)}")
    if len(paths) > 5:
        lines.append(f"  … and {len(paths) - 5} more paths")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("trace_path")
def _trace_path_summary(args: dict) -> str:
    return f"trace_path({args.get('from', '?')} → {args.get('to', '?')})"


@_register("get_subgraph")
def _get_subgraph(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Get a subgraph around a symbol, extending N hops in the knowledge graph."""
    name = args.get("name", "")
    depth = min(args.get("depth", 2), 4)  # max 4 hops

    if not name:
        return ToolResult(success=False, content="Missing required parameter: 'name'.")

    root = rg.workspace_root
    _ensure_knowledge_graph(root)

    from core.knowledge_graph import get_subgraph as _kg_get_subgraph
    sub = _kg_get_subgraph(name, depth)

    if not sub["nodes"]:
        return ToolResult(
            success=True,
            content=f"No subgraph found for '{name}'.",
        )

    lines: list[str] = [
        f"Subgraph around '{name}' ({depth} hops):",
        f"  {len(sub['nodes'])} entities, {len(sub['edges'])} edges",
    ]

    # Group edges by kind
    by_kind: dict[str, list[dict]] = {}
    for e in sub["edges"]:
        by_kind.setdefault(e["kind"], []).append(e)

    for kind, edges in sorted(by_kind.items()):
        lines.append(f"\n  [{kind}] ({len(edges)} edges):")
        for e in edges[:8]:
            lines.append(f"    {e['source']} → {e['target']}  {e['file']}:{e['line']}")
        if len(edges) > 8:
            lines.append(f"    … and {len(edges) - 8} more")

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("get_subgraph")
def _get_subgraph_summary(args: dict) -> str:
    return f"get_subgraph({args.get('name', '?')}, depth={args.get('depth', 2)})"


# ---------------------------------------------------------------------------\n# fetch_url -- fetch a web page and return its content\n# ---------------------------------------------------------------------------

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
