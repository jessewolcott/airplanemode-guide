# Background downloader (`download.py`)

Goal: get the guide onto the box while it's online (normally during the platform's
`scripts/update.sh` at home), without blocking the app, and never use a broken file.

## Constants

```python
DEFAULT_ZIM_URL = "https://download.kiwix.org/zim/wikivoyage/wikivoyage_en_all_nopic_2026-09.zim"
PINNED_SHA256   = {DEFAULT_ZIM_URL: "4305c50550f065d83e70f75fd1d2016a694f5d67aa1e0ef00fdf6bb74fc092ac"}
DATED = re.compile(r"^(?P<base>.+)_(?P<date>\d{4}-\d{2})$")   # on the file stem
CHUNK = 1 << 20                                               # sha256 read size
```

`zim_name(url)` (31-35): the last path component of the URL; must end in `.zim`, contain no
`/`, not start with `.`, else `ValueError("ZIM_URL must point at a .zim file: ...")`.

## Wiring

`app.py:44,55`: `ZIM_URLS = os.getenv("ZIM_URL", DEFAULT_ZIM_URL).split()` (so `ZIM_URL=""`
gives `[]`), `Downloader(ZIM_URLS, ZIM_DIR, library.reload, client, ZIM_SHA256)`. The lifespan
starts `downloader.run()` as a task unless `state == "off"`. It shares the app's
`httpx.AsyncClient` but passes per-request timeouts.

## State machine

| state | meaning |
|---|---|
| `off` | no URLs configured; never changes; no task started |
| `idle` | constructed with URLs, `run()` not yet at work |
| `downloading` | streaming into `<name>.zim.part` |
| `verifying` | computing SHA-256 of the `.part` |
| `waiting` | last attempt failed; sleeping before a retry (`error`, `retry_in_s` set) |
| `done` | every configured file exists in `ZIM_DIR` |

`status()` (55-61) → `{state, file, done, total, percent, rate, eta_s, error, retry_in_s}`:
`percent = round(100*done/total, 1)` when total known; `rate` bytes/s (rounded);
`eta_s = (total-done)/rate`; `retry_in_s` counts down to `next_try`. This dict is
`/health`'s `download` field, which the page's banner renders.

## `run()` (66-88)

```
attempt = 0
loop:
    todo = missing()                 # configured URLs whose final .zim isn't in ZIM_DIR
    if none: state = "done" (unless off); clear error/next_try; return
    try: fetch(todo[0]); attempt = 0
    except CancelledError: raise
    except Exception as e:
        state = "waiting"; error = short_error(e)   # ≤160 chars
        wait = retry_after[min(attempt, len-1)]     # default (30, 60, 300, 900) seconds
        attempt += 1; log warning; next_try = now + wait; sleep(wait)
```

So offline the box retries quietly forever, settling at every 15 minutes. Files are fetched one
at a time in `ZIM_URL` order. "Exists" is checked by final file name only, so a file that's
already there is never re-verified or re-downloaded.

## `fetch(url)` (105-162): resumable download

1. `part = ZIM_DIR/<name>.zim.part`; `have` = its size (0 if absent).
   `state="downloading"`, `done=have`, `total=None`, `rate=None`.
2. `GET url` streaming, `follow_redirects=True` (Kiwix's download host redirects to mirrors),
   `timeout=httpx.Timeout(30, read=60)`, with `Range: bytes={have}-` if `have > 0`.
3. Response handling:
   - **416** with `have`: the part is already complete → `total = have`, nothing to write.
   - **206** with `have`: parse `Content-Range: bytes S-E/T`; if missing or `S != have` →
     `IOError("server sent the wrong range")`; `total = T`; append.
   - **200**: if `have` was > 0 the server ignored Range → log and start over (`have = 0`, file
     opened `"wb"`); `total` from `Content-Length` if present (else unknown).
   - anything else → `IOError("HTTP <code>")`.
4. **Disk check** when `total` is known: needs `total - have` bytes plus 50 MB headroom free in
   `ZIM_DIR`, else `IOError("not enough disk space: need X MB, have Y MB")`.
5. Stream with `r.aiter_bytes()`, writing each chunk as it arrives (a dropped connection loses
   nothing already received; the next attempt resumes from the `.part` size). `done` grows per
   chunk. `rate` is an EWMA updated at most once a second: `rate = 0.7*rate + 0.3*instant`.
6. **Size check**: if `total` is known and the `.part` size differs →
   `IOError("incomplete: S of T bytes")` (the part is kept; the retry resumes).
7. **SHA-256** (`expected_sha`, 90-103), first match wins:
   1. `ZIM_SHA256` env (lower-cased) — used for *any* URL;
   2. `PINNED_SHA256[url]`;
   3. `GET <url>.sha256` (20 s timeout, redirects followed): the first 64 hex chars at the start
      of the body (Kiwix's `sha256sum` format `"<hash>  <file>"`);
   4. none → no hash check (only the size check protects the file).
   If there's a hash: `state="verifying"`, hash computed in a worker thread
   (`asyncio.to_thread(sha256_file, part)`, 1 MB reads). Mismatch → delete the `.part`,
   `done=0`, `IOError("checksum mismatch; the file was deleted and will be fetched again")`.
8. `part.replace(final)` (atomic rename), log, `on_done()` (= `library.reload()`, so the new ZIM
   is served immediately and `content_ready` flips true), `remove_older(final)`, `on_done()`
   again (drops the removed file from the library).

## Updating to a newer dated ZIM: `remove_older(new)` (165-174)

If the new file's stem matches `DATED` (`<base>_<YYYY-MM>`), every `<base>_*.zim` in the same
folder whose own stem matches with the **same base** and an **earlier** date (string compare,
fine for `YYYY-MM`) is deleted. Other ZIMs (different base) are untouched. Undated names are
never cleaned up. Because the new file is opened by `reload()` first, there's no window with no
content. (On Linux, deleting a file libzim still has open is safe; the next `reload` drops it.)

Example: setting `ZIM_URL=.../wikivoyage_en_all_nopic_2027-03.zim` downloads it next to
`..._2026-09.zim`, then removes the 2026-09 file. The ZIM id changes, so old `z/<id>/...` links
and cached ask passages from before the switch 404.

## `content_ready`

`/health.content_ready` is `library.ready` = at least one ZIM opened. It's independent of the
downloader: a hand-copied `.zim` makes the app ready with `ZIM_URL=""`. While a first download
runs, `content_ready` is false and the page shows the progress banner; search/ask return 503 with
"still downloading (N%)".

## Tests covering this

- `test_download_resumes_verifies_and_replaces_older`: a fake mirror drops the connection after
  12,000 of 25,000 bytes; asserts state `waiting`, `.part` has 12,000 bytes, then the retry sends
  `Range: bytes=12000-`, the final file matches, the older `voyage_en_all_2025-01.zim` is
  removed, `other_2020-01.zim` is kept, `on_done` called, state `done`. Uses the mirror's
  `.sha256` endpoint for verification.
- `test_download_restarts_when_range_ignored_and_rejects_bad_checksum`: a server that ignores
  Range yields a clean, correct restart; `sha256="0"*64` → `IOError` with "checksum", neither
  `.zim` nor `.part` left; `zim_name` rejects a non-`.zim` URL.

## Weak spots

- `missing()` (which calls `zim_name`) runs **outside** the `try` in `run()`. A malformed
  `ZIM_URL` (not ending in `.zim`) raises out of the background task: the task dies with an
  unretrieved exception, the state stays `idle`, nothing is shown to the user except a log line
  at task garbage collection. The docstring's "Never raises" doesn't hold for that case.
- `ZIM_SHA256` applies to every URL, so with several URLs set it makes all but one fail
  verification forever (each failure deletes the `.part` and re-downloads). Leave it empty when
  using several URLs; rely on Kiwix's `.sha256` files.
- With no hash available and a server that sends no `Content-Length`, a truncated stream that
  ends "cleanly" would be accepted. Kiwix always sends both, so this is theoretical.
- `on_done` (`library.reload`) runs synchronously on the event loop (twice per download), so
  opening the new archive blocks other requests for as long as that takes. Not measured; worth
  checking on the R6S.
