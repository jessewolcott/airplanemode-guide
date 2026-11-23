# Travel Guide: how it's built (LLM / engineer reference)

Checked-in notes (`.dockerignore` lists `llm`, so none of this reaches the image). Written from the code as of version 0.1.0
(2026-11). If the code and these notes disagree, the code wins: fix the notes.

## Overview

Travel Guide is an AirplaneMode app: a single FastAPI process (`app.py`) that serves the whole
English Wikivoyage offline from Kiwix ZIM files, read in place with **libzim** (no kiwix-serve,
no unpacking). A background **downloader** (`download.py`) fetches the pinned ZIM while the box is
online (resumable, size + SHA-256 checked) and reports progress in `GET /health`, whose
`content_ready` flag the platform's `update.sh` waits on. Articles are served at
`z/<zim id>/<path>` after being **sanitized** with lxml and wrapped in the app's own page under a
no-script CSP, because the app shares an origin with the router's admin panel. Users can
**search** (libzim title suggestions + Xapian full-text) or **ask**: a deterministic pipeline
(`ask.py`) finds up to 3 articles (named places first, then full-text hits), splits them into
~110-word passages tagged with their Wikivoyage section, ranks them with BM25 plus a
section-intent boost, and returns the best ones within a token budget immediately. Optionally the
shared llama.cpp model then writes a 2–5 sentence answer from those passages only, in
grammar-enforced JSON, and a deterministic guard (`check_answer()`) drops it if it contains
numbers or too many words that aren't in the passages. Without a model, the passages are the
answer. The app runs at `/guide/` behind Caddy with a 256 MB memory limit, so all URLs are
relative and heavy lxml work is throttled and followed by `malloc_trim`.

## Files in this folder

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | Repo layout, process model, module responsibilities, request flows, config/env vars, storage/volume layout, how the platform wires it in, all endpoints with inputs/outputs |
| [library-and-articles.md](library-and-articles.md) | `library.py`: opening ZIMs, ids, reload, title lookup, suggestions, full-text search, snippets, article serving at `z/<id>/<path>`, redirects, `sanitize()`, the article page, CSPs, `RAW_OK` raw files, `prefix_for()` |
| [downloader.md](downloader.md) | `download.py`: state machine, Range resume, size/disk checks, SHA-256 sources, progress/ETA, retry/backoff, replacing older dated files, `content_ready` |
| [ask-pipeline.md](ask-pipeline.md) | `ask.py` + the ask/answer endpoints end to end: place lookup, full-text search, passage splitting, BM25 + intents, selection within budget, prompt, JSON schema, `check_answer()`, statuses and fallbacks, stats |
| [page.md](page.md) | `static/index.html` (Ask/Search tabs, suggestions, banner, answer + passages) and the article view (`render_article` + `static/article.css`) |
| [memory.md](memory.md) | The 256 MB budget: `_heavy()`, `heavy` semaphore, `malloc_trim`, `MALLOC_ARENA_MAX`, cluster cache, measured numbers, how to re-measure |
| [testing-and-release.md](testing-and-release.md) | `tests/test_guide.py` (fake ZIM, stubs, what each test covers), running in Docker, CI workflow, releasing, platform pinning |
| [gotchas-and-extending.md](gotchas-and-extending.md) | Known weak spots, code-level gotchas found while writing these notes, and recipes: new default ZIM, extra ZIMs, ranking changes, new endpoints |

## Ground rules (from the repo's `CLAUDE.md`, repeated because they bite)

- Relative URLs only: the app is mounted at `/guide/` (Caddy `handle_path` strips the prefix).
- Never serve ZIM HTML as is: `sanitize()` + `ARTICLE_CSP`. Raw files only for `RAW_OK`
  mimetypes, never scripts, always `RAW_CSP` (sandbox).
- `/api/ask` must work without the model; model output must pass `check_answer()`.
- 256 MB memory limit: lxml work goes through `_heavy()`; re-measure `RssAnon` after changes.
- A new default ZIM is pinned in `download.py` together with its sha256.
- Tests need Python 3.11+ (libzim); local Windows Python is 3.10, so run them in Docker.
- Pushing a `v*` tag publishes an image: ask Jesse first.
