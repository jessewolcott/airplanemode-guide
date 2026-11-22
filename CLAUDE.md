# Travel Guide (AirplaneMode app): notes for Claude

Offline Wikivoyage. Kiwix ZIM files read with libzim (`library.py`), a background downloader
(`download.py`), and "ask" (`ask.py`): deterministic passage retrieval (place lookup, full-text
search, BM25 with Wikivoyage section intents), then an optional model answer checked against the
passages. `app.py` is the FastAPI app; `static/` is the page and the article stylesheet.

Part of AirplaneMode: the platform repo is `../airplanemode`. Read its `CLAUDE.md` for the
environment notes (H:\Github on exFAT, Docker Desktop, port 8000 reserved, commit attribution)
and `docs/app-contract.md` for the rules this app must follow.

## Commands

```powershell
docker compose up -d --build        # standalone with its own 0.5B model -> http://localhost:8767
# tests: libzim needs Python 3.11+, local Python is 3.10, so run them in Docker
docker run --rm -v "H:\Github\airplanemode\airplanemode-guide:/src" -w /src python:3.12-slim sh -c "pip install -q -r requirements.txt && python tests/test_guide.py"
```

In Git Bash, set `MSYS_NO_PATHCONV=1` for `docker run -v`, and keep scratch files in the repo
or the session scratchpad: Docker can't see Git Bash's `/tmp`.

## Rules

- **Relative URLs only** in pages: the app runs at `/guide/`. Articles live at
  `z/<zim id>/<path>` so ZIM-internal relative links resolve on their own; `prefix_for(path)`
  gets back to the app root.
- **Never serve ZIM HTML as is.** Everything goes through `sanitize()` + the article CSP. Raw
  files: only `RAW_OK` mimetypes, never scripts, always with the sandbox CSP. The app shares an
  origin with the admin panel.
- **Works without the model**: `/api/ask` returns passages without touching it. The model's
  answer must pass `check_answer()` (numbers and content words from the passages) or it's dropped.
- **Memory**: 256 MB limit. Heavy lxml work goes through `_heavy()` (2 at a time + malloc_trim).
  Re-measure `RssAnon` (`/proc/1/status`) after changing parsing or caching.
- Pin a new default ZIM in `download.py` together with its sha256 (Kiwix publishes `<url>.sha256`).

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-guide:X.Y.Z`
(arm64 + amd64). Then add or bump it in `../airplanemode/compose.yaml`.
**Ask Jesse before pushing a release tag.** Repo: github.com/jessewolcott/airplanemode-guide.

## Not verified yet

- Speed on the R6S: retrieval is ~0.1 s on a PC; the model's answer (≈850-token prompt, ~100
  tokens out) took 3–6 s with the 1.5B model on a PC, so expect tens of seconds on the box.
- Memory on arm64.

## Known weak spots (ask)

- Retrieval takes at most 3 articles: named places first, then full-text order. A question
  about a thing with its own article ("Kansai airport" → *Kansai International Airport*) may
  not get that article when two places are named. Sorting hits by title overlap was tried
  and pulled *Lisbon Airport*'s food section above *Lisbon › Eat*, so it was dropped.
- The 1.5B model sometimes answers from a neighbouring passage: for "Kansai airport to Osaka
  city centre" it can describe Itami airport's monorail. The grounding guard can't catch that
  (the words are in the passages). Use that question when working on ranking.
- Python on Windows reads piped text as cp1252: decode responses as UTF-8 when checking
  answers from Git Bash, or you'll see mojibake that isn't there.
