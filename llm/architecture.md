# Architecture

## Repo layout

```
app.py              FastAPI app: config, lifespan, endpoints, model call, _heavy()   (265 lines)
library.py          ZIM reading (libzim), search, sanitize/render articles, passages() (415)
ask.py              "ask": article finding, BM25 ranking, prompt, check_answer()       (283)
download.py         background ZIM downloader                                         (187)
static/index.html   the single-page UI (inline CSS + JS, no external resources)
static/article.css  stylesheet for article pages
static/icon.svg     favicon (also docs/icon.svg for the README)
tests/test_guide.py all tests; plain script, no pytest
Dockerfile          python:3.12-slim, uvicorn on :8000
compose.yaml        standalone dev stack: guide + its own llama.cpp (0.5B model) on :8767
app.yaml            AirplaneMode app manifest (id guide, path /guide/, memory 256m, llm optional)
requirements.txt    fastapi 0.115.6, uvicorn 0.34.0, httpx 0.28.1, libzim 4.1.0, lxml 6.1.3
.github/workflows/image.yml   tests + multi-arch image build/push
.vscode/tasks.json  standalone up/down, tests in Docker
docs/screenshots/   home.png, ask.png, article.png (README images)
```

`.dockerignore` keeps `tests`, `*.md`, `compose.yaml`, `.github`, `.vscode`, screenshots, and
any `*.zim`/`*.zim.part` out of the image. So the image does **not** contain the tests (the VS
Code test task mounts the repo instead).

## Process model

One uvicorn worker, one Python process (`CMD ["uvicorn", "app:app", "--host", "0.0.0.0",
"--port", "8000"]`, `Dockerfile:9`). Module-level singletons in `app.py:53-58`:

| Object | Purpose |
|---|---|
| `client = httpx.AsyncClient(timeout=LLM_TIMEOUT)` | shared by the model calls, `llm_status()` and the downloader |
| `library = Library(ZIM_DIR, CLUSTER_CACHE_MB)` | the open ZIM archives |
| `downloader = Downloader(ZIM_URLS, ZIM_DIR, library.reload, client, ZIM_SHA256)` | background fetch; calls `library.reload` when a file lands |
| `model_turn = asyncio.Semaphore(1)` | one model call at a time from this app (the model is shared by all apps) |
| `asks: OrderedDict[str, dict]` | pending/answered asks by id, capped at 100 (FIFO eviction) |
| `STATS` | `asks` count, `answers` Counter by status, `rejected_by` Counter by reason |

Concurrency:
- Endpoints are `async`. Blocking libzim/lxml work is pushed to Starlette's thread pool with
  `run_in_threadpool` (suggest, search, ask's `gather`, article rendering, the initial
  `library.reload`).
- `heavy = threading.BoundedSemaphore(2)` (`library.py:63`) caps concurrent lxml parses
  (article render + ask gather share it), see [memory.md](memory.md).
- Each `Zim` has a `threading.Lock` around libzim search/suggestion objects (they aren't
  thread-safe to share). `Library.lock` only guards the swap of the `zims` dict on reload.
- Not everything is off the event loop: `library.entry()` and `bytes(item.content)` for raw
  files run directly in `zim_entry` (`app.py:237,255`), and the downloader's `on_done`
  (`library.reload`) runs synchronously in the event loop. Both are usually quick.

Lifespan (`app.py:61-68`): make `ZIM_DIR`, `library.reload()` in the thread pool, then start
`downloader.run()` as a task unless the downloader is `off` (no URLs). The task is cancelled on
shutdown.

FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled (`app.py:71`).

## Configuration (env vars)

| Var | Default | Where | Meaning |
|---|---|---|---|
| `ZIM_DIR` | `/data/zim` (also set in Dockerfile) | app.py:43 | every `*.zim` here is served |
| `ZIM_URL` | `DEFAULT_ZIM_URL` (Wikivoyage en all nopic 2026-09) | app.py:44 | space-separated list; empty string = never download |
| `ZIM_SHA256` | `""` | app.py:45 | expected hash; **applies to every URL** in `ZIM_URL` (see gotchas) |
| `LLM_URL` | `http://llm:8080` | app.py:46 | OpenAI-compatible base URL; empty = no model |
| `LLM_TIMEOUT` | `90` s | app.py:47 | model answer timeout (also the httpx client default) |
| `LLM_WAIT` | `3` s | app.py:48 | wait for `model_turn`, else `busy` (not in app.yaml) |
| `PASSAGE_TOKENS` | `900` | app.py:49 | prompt budget for passages |
| `ANSWER_TOKENS` | `220` | app.py:50 | `max_tokens` for the model (not in app.yaml) |
| `CLUSTER_CACHE_MB` | `8` | app.py:51 | libzim decompressed-cluster cache |
| `MALLOC_ARENA_MAX` | `2` | Dockerfile | glibc arenas (memory) |
| `PYTHONUNBUFFERED` | `1` | Dockerfile | logs |

Standalone compose also accepts `MODEL` (llama.cpp `-hf` model, default
`Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M`) and `PORT` (default 8767).

## Storage / volume layout

```
/data                      volume (guide-data in both composes)
└── zim/                   ZIM_DIR
    ├── wikivoyage_en_all_nopic_2026-09.zim        served; id = "wikivoyage_en_all_nopic_2026-09"
    ├── wikivoyage_en_all_nopic_2026-12.zim.part   in-progress download (ignored by the library)
    └── anything_else.zim                          dropped in by hand: also served
```

- The ZIM id is the file name without `.zim` (`Zim.id = path.stem`). It appears in every article
  URL, so renaming a file breaks bookmarks and cached passage links.
- The default file is 272,495,142 bytes (~260 MB) and includes a full-text (Xapian) index.
- Nothing else is persisted: asks, answers and stats live in memory and reset on restart.
- The standalone compose has a second volume, `models`, for llama.cpp's model cache.

## How the platform wires it in (`../airplanemode`)

- `compose.yaml`: service `guide`, image `ghcr.io/jessewolcott/airplanemode-guide:0.1.0`,
  `LLM_URL: ${LLM_URL-http://llm:8080}`, volume `guide-data:/data`,
  `mem_limit: ${GUIDE_MEM:-256m}`, `restart: unless-stopped`. Container port 8000, not published.
- `Caddyfile`: `redir /guide /guide/` and `handle_path /guide/* { reverse_proxy guide:8000 }`.
  `handle_path` strips `/guide`, so the app sees `/`, `/api/...`, `/z/...`.
- `portal/apps.json`: entry `id: guide`, monogram `TG`, color `#5ec4b6`, `uses_llm: true`.
- `scripts/update.sh`: after `up`, polls `http://guide:8000/health` from inside the `admin`
  container until `content_ready` is true (every 5 s, gives up after 240 tries = 20 min).
- CI in the platform repo checks the image pin, Caddy route and apps.json entry all exist.
- App rules: `../airplanemode/docs/app-contract.md` (reserved paths `/api`, `/admin` are
  platform-level; inside the app, `api/` is fine because Caddy strips `/guide`).

## Endpoints

All paths below are relative to the app root (`/guide/` on the box).

| Method + path | Input | Output | Notes |
|---|---|---|---|
| `GET /` | — | `static/index.html` | `Cache-Control: no-cache`. Always 200, even with no content. Reads `?q=` client-side. |
| `GET /static/...` | — | static files | `StaticFiles` mount (`app.py:260`) |
| `GET /health` | — | `{content_ready: bool, zims: [Zim.info()], download: Downloader.status(), llm: "ready"/"offline"/"none", model: str|null}` | `llm` cached 30 s; probes `GET {LLM_URL}/v1/models` with 3 s timeout |
| `GET /api/stats` | — | `{asks, answers: {status: n}, rejected_by: {reason: n}, accept_rate}` | `accept_rate = ok / all answer outcomes` (incl. busy/timeout/offline), `null` if none |
| `GET /api/suggest?q=` | `q` trimmed, cut to 100 chars | `{results: [{zim, path, title}]}` (≤ 8) | empty `q` → `[]`; no content → 503 |
| `GET /api/search?q=` | `q` trimmed, cut to 200 chars | `{results: [{zim, path, title, snippet, total}]}` (≤ 25) | snippet is HTML with only `<b>`; `total` = that ZIM's estimated matches |
| `POST /api/ask` | `{question}` 3–300 chars (pydantic → 422 otherwise) | `{id: hex|null, articles: [{zim, path, title}], passages: [{zim, path, title, section, anchor, text, place, score}], model: bool}` | 503 if no content. `id` null and `model` false when no passages |
| `POST /api/answer` | `{id}` | `{status, ...}` see below | 404 `"That question has expired; ask again."` for unknown/evicted id |
| `GET /z/{zim_id}/{path:path}` | — | sanitized article page, a 302 for redirects, or a raw file | see [library-and-articles.md](library-and-articles.md) |

`/api/answer` statuses (`app.py:178-219`):

| status | when | extra fields |
|---|---|---|
| `ok` | model replied, JSON parsed, `check_answer()` passed | `answer`, `sources` (1-based passage numbers), `seconds`; cached on the ask |
| `none` | `LLM_URL` empty | `detail` |
| `busy` | couldn't get `model_turn` within `LLM_WAIT` | `detail` |
| `rejected` | bad JSON / not a dict / `check_answer` raised | `detail` |
| `timeout` | `httpx.TimeoutException` / `asyncio.TimeoutError` | `detail` |
| `offline` | any other exception (connect error, HTTP error status, malformed response) | `detail` |

503 errors (`not_ready()`, `app.py:74-82`) carry a human message in `detail` depending on the
downloader: "still downloading (N%)" while downloading/verifying with a known percent,
"hasn't been downloaded yet…" while `waiting`, else "No guide content on this box yet."

## Request flows (summary)

- **Article**: `GET z/<id>/<path>` → `library.entry` → redirect? 302 relative → HTML? `_heavy(_render)`
  in thread pool → `article_html` → `render_article` (lxml parse, `article_root`, `sanitize`,
  wrap) → response with `ARTICLE_CSP` → else `RAW_OK`? bytes with `RAW_CSP` : 404.
- **Ask**: `POST api/ask` → `_heavy(asking.gather)` in thread pool → find articles → split →
  score → select → store in `asks` → respond. Page then `POST api/answer` → semaphore → `chat()`
  → `json.loads` → `check_answer` → respond. Details in [ask-pipeline.md](ask-pipeline.md).
- **Download**: lifespan task → `Downloader.run()` loop → `fetch()` → `.part` → verify → rename →
  `library.reload()` → `remove_older()` → `library.reload()`. Details in [downloader.md](downloader.md).
