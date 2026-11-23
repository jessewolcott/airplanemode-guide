# Memory management

The platform gives the app **256 MB** (`app.yaml: memory: 256m`; platform compose
`mem_limit: ${GUIDE_MEM:-256m}`; standalone compose `mem_limit: 256m`). The box (R6S 8 GB)
also runs the shared model and the other apps, so the guide aims well under that. (It isn't
installed on the GL.iNet lite profile, platform decision 0017.)

## Where memory goes

| Consumer | Control |
|---|---|
| libzim decompressed-cluster cache | `set_cluster_cache_max_size(CLUSTER_CACHE_MB, default 8)` in `Library.__init__` (`library.py:119-123`) |
| Xapian searcher per ZIM | created once and reused (`Zim.searcher()`); suggestion searchers are created per call |
| lxml parse trees | ~10 MB for a moment per big article; bounded by `heavy` (2) + `malloc_trim` |
| glibc per-thread arenas | `MALLOC_ARENA_MAX=2` in the Dockerfile (Starlette's thread pool otherwise leaves freed memory in many arenas) |
| `asks` cache | ≤ 100 entries × ≤ 6 passages (~110 words each) + answers: small |
| ZIM file itself | read via libzim; file pages are page cache, not anonymous memory |

## `_heavy()` and `heavy`

```python
# library.py:63
heavy = threading.BoundedSemaphore(2)

# library.py:64-75
_libc = ctypes.CDLL("libc.so.6")   # None if not glibc (Windows, macOS, musl)
def trim_memory():
    if _libc is not None: _libc.malloc_trim(0)   # errors ignored

# app.py:223-228
def _heavy(fn, *args):
    with heavy:
        try:     return fn(*args)
        finally: trim_memory()
```

Used for the two lxml-heavy paths, always inside `run_in_threadpool`:
- article pages: `run_in_threadpool(_heavy, _render, z, zim_id, path)` (`app.py:248`);
- ask: `run_in_threadpool(_heavy, asking.gather, library, question, PASSAGE_TOKENS)`
  (`app.py:157`). One gather parses up to 3 articles sequentially while holding one slot.

So at most two parses run at once across both paths; further requests wait in their thread-pool
thread. `malloc_trim(0)` after each returns freed heap to the OS (glibc otherwise keeps it, and
RSS would ratchet up to the peak). Suggest and search aren't wrapped (libzim/Xapian, no lxml).

## Measured (README, PC amd64, full English Wikivoyage open)

| | RSS |
|---|---|
| started, ZIM open | ~35 MB |
| after browsing, searching and asking | ~75 MB anonymous, ~110 MB RSS |
| peak, 18 asks and page loads at once | 143 MB |

**Not measured on arm64 / the R6S yet** (CLAUDE.md "Not verified yet").

## Re-measuring (do this after changing parsing, caching or concurrency)

Anonymous memory is the number that counts against the cgroup limit's pressure; file-backed
pages from the ZIM can be reclaimed.

```powershell
docker compose up -d --build                     # standalone, http://localhost:8767
docker compose exec guide grep -E "VmRSS|RssAnon|RssFile" /proc/1/status
docker stats --no-stream                         # cgroup view
```

Then load it (several article pages, searches, and concurrent asks — e.g. fire ~18 requests at
once with a small script) and read the numbers again. Record new results in the README's Memory
table.

## Rules of thumb

- Any new code that builds lxml trees (or other big transient structures) per request should go
  through `_heavy()`.
- Don't cache parsed articles or passages across requests without measuring; the current design
  re-parses on every ask on purpose.
- Raising `heavy` above 2 or `CLUSTER_CACHE_MB` above 8 trades memory for speed; measure first.
- `trim_memory()` is a no-op on Windows/macOS, so measure in the Linux container, not locally.
