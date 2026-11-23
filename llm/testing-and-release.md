# Tests, build, CI and release

## `tests/test_guide.py`

A plain script (no pytest): `if __name__ == "__main__"` runs every module-level `test_*`
function in definition order and prints `ok  <name>`, then `all passed in N s`. The first failing
`assert` stops the run with a traceback.

### Setup (module level, before importing the app)

1. `TMP = tempfile.mkdtemp(prefix="guide-test-")` (not cleaned up); env:
   `ZIM_DIR=TMP/zim`, `ZIM_URL=""` (downloader `off`), `LLM_URL=http://llm.test`,
   `LLM_TIMEOUT=5`, `LLM_WAIT=0.2`. These must be set before `import app`, which reads env at
   import time.
2. **A real ZIM is built** with `libzim.writer.Creator(...).config_indexing(True, "eng")` (so it
   has a full-text index): id `testvoyage_en_all_2026-01`, main path `Portugal`, articles
   `Lisbon`, `Lisbon/Alfama`, `Porto`, `Osaka`, `Portugal`, plus `_res_/style.css` (text/css) and
   `_res_/evil.js` (application/javascript), a redirect `Lisboa → Lisbon`, and metadata
   (Title "Test Voyage", Source `en.wikivoyage.org`, Date `2026-01-01`, Language `eng`...).
   `page()` wraps bodies like mwoffliner: head scripts and stylesheet, `nav#mw-navigation`,
   `main#content`, `footer#footer`. `LISBON` deliberately contains attacks: `onclick`,
   `javascript:` link, `iframe`, `<style>`, a `data:` SVG image placeholder, `<svg onload>` with a
   script, an inline `<script>`, plus an absolute `/Porto` link, a `geo:` link and an external
   link.
3. `TestClient(guide.app).__enter__()` runs the lifespan (opens the ZIM).
4. **Model stub**: `FakeLLM` behind `httpx.MockTransport`; `guide.client` is replaced, and
   `_llm_cache` cleared. `/v1/models` → `fake-model`; chat replies `json.dumps(llm.reply)`, or
   raises `llm.reply` if it's an exception. Requests are recorded in `llm.requests`.
5. **Download stub**: `FakeMirror` serves bytes with Range support, a `.sha256` endpoint, an
   optional mid-stream drop (`cut_after`) and an "ignore Range" mode.

### What each test covers

| Test | Checks |
|---|---|
| `test_health` | `content_ready`, ZIM info (id, title, fulltext), download `off`, llm `ready` |
| `test_suggest_and_search` | redirects shown as target (no "Lisboa"), no duplicates, exact match first, full-text hit with `<b>` and no `<script>` in snippet, empty query |
| `test_article_is_sanitized` | CSP header; no script/onclick/onload/javascript:/iframe/style/data:image/svg/svg/nav/footer/[edit]; relative links kept; `/Porto` → `./Porto`; `../../static/article.css`, form `action="../../"`; external marked; `geo:` kept; attribution + source link |
| `test_subpage_and_redirect_and_raw_files` | subpage prefixes (`../Lisbon`, `../../../static/...`); `Lisboa` → 302 `Location: Lisbon`; CSS served with sandbox CSP; `.js` 404; unknown path / unknown zim 404 |
| `test_passages` | section labels incl. `Get in › By plane`; listings present; no script/chrome text; anchors on passages after the intro |
| `test_intents_and_places` | `intents()` and `place_candidates()` examples |
| `test_ask_finds_the_right_passages` | top passage Lisbon/Eat (also lowercase), Osaka/Get in; budget ≤ `PASSAGE_TOKENS`; nonsense → no passages, `id` null |
| `test_place_names_still_count_outside_their_own_article` | `score_passages` unit test: Kansai section beats Itami section |
| `test_answer_from_model` | `[1]` stripped, out-of-range source 9 dropped; request has `cache_prompt` + `json_schema`; prompt includes passages and question; second call served from cache |
| `test_answer_guards` | invented number → rejected; ungrounded → rejected; ConnectError → offline; ReadTimeout → timeout; unknown id 404; stats reasons |
| `test_answer_busy` | holds `model_turn`, calls via `ASGITransport` → `busy` after `LLM_WAIT` |
| `test_no_model` | `LLM_URL=""` → `model: false` with passages; answer `none` |
| `test_index_page_uses_relative_urls` | no absolute URLs or outside resources in index.html |
| `test_download_resumes_verifies_and_replaces_older` | drop at 12,000 bytes → `waiting`, resume with `Range: bytes=12000-`, verified, older dated file removed, unrelated kept |
| `test_download_restarts_when_range_ignored_and_rejects_bad_checksum` | clean restart when Range ignored; bad checksum deletes `.part`; non-`.zim` URL rejected |
| `test_not_ready_without_content` | empty library → health false, ask/search 503 with a "guide" message, `/` still 200 |

Tests mutate module globals (`guide.LLM_URL`, `library.zims`) and restore them in `finally`.
Order matters a little (`test_health` relies on the LLM cache reset done at setup).

### Running them

libzim needs **Python 3.11+**; Jesse's local Python is 3.10, so use Docker:

```powershell
# PowerShell (from CLAUDE.md)
docker run --rm -v "H:\Github\airplanemode\airplanemode-guide:/src" -w /src python:3.12-slim sh -c "pip install -q -r requirements.txt && python tests/test_guide.py"
```

```bash
# Git Bash: stop MSYS rewriting the -v path
MSYS_NO_PATHCONV=1 docker run --rm -v "H:/Github/airplanemode/airplanemode-guide:/src" -w /src python:3.12-slim sh -c "pip install -q -r requirements.txt && python tests/test_guide.py"
```

VS Code task "Tests (in Docker: libzim needs Python 3.11+)" (default test task) builds the app
image (`airplanemode-guide:test`) and runs `python tests/test_guide.py` in it with the repo
mounted read-only at `/src` (the image itself excludes `tests/` via `.dockerignore`). Faster on
repeat runs since dependencies are baked in.

Other notes: keep scratch files in the repo or the session scratchpad (Docker can't see Git
Bash's `/tmp`). When checking answers from Git Bash with Python, decode responses as UTF-8
(Windows Python reads pipes as cp1252 → fake mojibake).

### Manual / live testing

`docker compose up -d --build` → http://localhost:8767 with its own llama.cpp
(`ghcr.io/ggml-org/llama.cpp:server-b12602`, `-c 4096 -np 2`, default
`Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M`). First start downloads ~260 MB of guide and ~400 MB of
model into volumes. `MODEL=unsloth/gemma-4-E2B-it-GGUF:Q4_K_M` matches the box's model (platform decision 0018);
`PORT=9000` changes the port; uncomment `ZIM_URL` in compose for the 80 MB Europe ZIM. Use the
"Kansai airport to Osaka city centre" question when working on ranking. Check `GET api/stats`
for accept/reject rates. Inside the platform dev stack (`../airplanemode`, compose.dev.yaml) the
app is built from this folder and served at http://localhost:8080/guide/.

## Docker image

`Dockerfile`: `python:3.12-slim`, `pip install -r requirements.txt` (libzim and lxml ship
manylinux wheels for amd64 and arm64), copy the source, `ENV ZIM_DIR=/data/zim
MALLOC_ARENA_MAX=2 PYTHONUNBUFFERED=1`, `EXPOSE 8000`, uvicorn. The app manifest says port 8000;
remember Windows reserves host port 8000, so publish elsewhere (8767 standalone).

## CI (`.github/workflows/image.yml`)

Triggers: push to `main`, tags `v*`, pull requests. Permissions `contents: read`,
`packages: write`. One job on `ubuntu-latest`:
1. `actions/checkout@v7`, `actions/setup-python@v7` (3.12).
2. `pip install -r requirements.txt && python tests/test_guide.py`.
3. QEMU + Buildx (`docker/setup-qemu-action@v4`, `setup-buildx-action@v4`).
4. GHCR login (not on PRs).
5. `docker/metadata-action@v6`: tags `type=ref,event=branch` (→ `:main`) and
   `type=semver,pattern={{version}}` (tag `v1.2.3` → `:1.2.3`).
6. `docker/build-push-action@v7`: `linux/arm64,linux/amd64`, push unless PR.

Image: `ghcr.io/jessewolcott/airplanemode-guide`.

## Releasing

1. Make sure CI on `main` is green.
2. **Ask Jesse before pushing the tag** (tags publish images).
3. `git tag vX.Y.Z && git push origin vX.Y.Z` → CI publishes `:X.Y.Z` (arm64 + amd64).
4. In `../airplanemode/compose.yaml`, bump `image: ghcr.io/jessewolcott/airplanemode-guide:X.Y.Z`
   (pinned, never `latest`); the platform's CI checks the image, Caddy route and
   `portal/apps.json` entry. Commit there.
5. Current pin: `0.1.1`.
