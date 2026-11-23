# Known weak spots, gotchas, and how to extend

## Known weak spots (from the repo's `CLAUDE.md`)

- **Retrieval takes at most 3 articles**: named places first (max 2 via title lookup), then
  full-text order. A question about a thing with its own article ("Kansai airport" →
  *Kansai International Airport*) may not get that article when two places are named. Sorting
  hits by title overlap was tried and pulled *Lisbon Airport*'s food section above
  *Lisbon › Eat*, so it was dropped.
- **The old 1.5B model (Qwen2.5) sometimes answers from a neighbouring passage**: for "Kansai airport to Osaka
  city centre" it can describe Itami airport's monorail. `check_answer()` can't catch that (the
  words are in the passages). Use that question when working on ranking.
- **Not verified on hardware**: retrieval ~0.1 s on a PC; the answer (≈850-token prompt, ~100
  tokens out) took 3–6 s with the old 1.5B model on a PC (Gemma 4 E2B, the box's model since
  platform decision 0018, not timed here), so expect tens of seconds on the R6S
  (`LLM_TIMEOUT` 90 s is sized for that). Memory on arm64 unmeasured.
- Windows Python reads piped text as cp1252: decode responses as UTF-8 when checking answers
  from Git Bash.

## Code-level gotchas (found reading the code)

Ask / ranking
- `title_entry` tries only `title`, `Title-first-letter`, and `str.title()`. Multi-word
  lowercase names with lowercase particles ("rio de janeiro") don't resolve by title; they
  depend on the full-text "title appears in question" path, which skips subpages (`/` in path).
- `place_candidates` treats any capitalised run as a place candidate, including the first word
  of a sentence ("Where" is a stopword, but "Hiking near Cape Town" would try "Hiking"). Each
  candidate costs a title lookup per ZIM; the loop stops after 2 hits.
- `passages()` keeps the previous anchor when a heading has no `id`, so a passage's `#anchor`
  link can point at the previous section. mwoffliner output puts ids on headings, so it's fine
  for Wikivoyage.
- Intro passages have section `""`: they never match an intent, so they get ×0.6 whenever the
  question has an intent.
- The intent bonus (+2.5) is added even when no question term occurs in the passage, so with an
  intent, the whole matching section of a place article can outrank keyword hits elsewhere. Any
  passage with score > 0 can be selected.
- `tokens()` is `len/4 + 1`: an estimate. Non-English ZIMs or text heavy in numbers/symbols will
  be off; the model context in standalone compose is 4096 (`-c 4096`) shared by 2 slots
  (`-np 2`), and depending on the llama.cpp build each slot may get only half of it; the box's
  model settings live in the platform repo. Don't raise `PASSAGE_TOKENS` without checking.
- `check_answer` allows any single-digit number, and compares numbers as strings after removing
  commas (`7` km fine, `1.85` must appear as `1.85`). A model that reformats `1.85 euro` as
  `€1.85` passes; `1,85` (European decimal) → `185` fails.
- Only `ok` answers are cached; re-calling `/api/answer` on a rejected id re-runs the model.
- `asks` eviction is FIFO by insertion: a question older than 100 asks ago → 404 "expired"
  (the page then shows the error in place of the answer note).
- `accept_rate` in `/api/stats` counts busy/timeout/offline in the denominator.

Serving / library
- `library.entry()` and raw file `bytes(item.content)` run on the event loop (not the thread
  pool). Fine for small files; a large image would block briefly.
- `article_html` docstring says 3-tuple; it returns 4.
- `ZIM_SHA256` applies to every URL in `ZIM_URL` (multi-URL setups must leave it empty).
- A malformed `ZIM_URL` (not `*.zim`) kills the downloader task silently (`missing()` is outside
  the try in `run()`); state stays `idle`.
- `/health`'s `llm` field is cached 30 s, and the page stops polling `/health` once content is
  ready, so the page's `llm` value is from page load.
- Renaming or replacing a ZIM changes its id; old article URLs and passage links 404.
- The ZIM's own CSS is dropped (`link` in `DROP_TAGS`); all styling comes from `article.css`.
  A non-Wikivoyage ZIM may look rough.
- The footer's "Online version" link assumes `https://<Source>/wiki/<path>` (MediaWiki).
- `trim_memory()` does nothing outside glibc (Windows/macOS/musl).

Environment (from the platform notes)
- Port 8000 is reserved on Jesse's Windows machine: standalone uses 8767.
- Git Bash: `MSYS_NO_PATHCONV=1` for `docker run -v`; Docker can't see Git Bash's `/tmp`.
- H: is exFAT: new clones need `git config --global --add safe.directory ...`.

## How to extend

### Pin a new default ZIM (e.g. a newer Wikivoyage dump)

1. Pick the dated file on https://download.kiwix.org/zim/wikivoyage/ (keep the `nopic` variant
   unless the size budget changes: `maxi` with pictures is ~1 GB).
2. Get its hash from `<url>.sha256` (Kiwix publishes one per file).
3. In `download.py`: update `DEFAULT_ZIM_URL` **and** `PINNED_SHA256` together.
4. Update README's Content table (URL, byte size) and any sizes mentioned in `app.yaml`/README.
5. Run the tests (they don't download), then try it standalone: the box keeps the old dated file
   until the new one is verified and opened, then `remove_older()` deletes it.
6. Release a new version and bump the platform pin.

### More content / other ZIMs

- Any `*.zim` dropped into `/data/zim` is opened on the next start (or the next
  `library.reload()`, which only happens after a download). Search, suggestions and ask cover all
  ZIMs; results are concatenated in sorted file-name order, so the first ZIM's hits come first.
- Or set `ZIM_URL` to several space-separated URLs (leave `ZIM_SHA256` empty).
- Ask's `INTENTS`/`SECTION_PREFIX` are Wikivoyage section names; a non-Wikivoyage ZIM (say a
  Wikipedia subset) still works through BM25 and the place boost, without intent matching.
- If a ZIM's HTML has a different layout, check `article_root()`'s XPath order and the
  `DROP_*` sets; add a fixture page shaped like it to the tests.
- New raw mimetypes: extend `RAW_OK` only for passive content, never anything script-capable
  (HTML, JS, XML/XHTML). SVG is already allowed and relies on `RAW_CSP`'s `sandbox`.

### Ranking changes

Knobs (all in `ask.py` unless noted):

| Knob | Where | Value |
|---|---|---|
| articles per question | `MAX_ARTICLES` | 3 (title hits stop at 2) |
| full-text hits considered | `find_articles` → `lib.search(query, limit=10)` | 10 |
| passage size | `library.passages(max_words=110)` | 110 words, min block 30 chars |
| dedupe key | `gather` | first 160 chars |
| BM25 | `score_passages(k1=1.4, b=0.75)` | |
| intent bonus / penalty | `score_passages` | +2.5 / ×0.6 |
| place boost | `score_passages` | ×1.6 |
| short passage penalty | `score_passages` | < 15 words ×0.7 |
| passages / budget | `gather(max_passages=6)`, `PASSAGE_TOKENS` | 6 / 900 tokens |
| intent vocabulary | `INTENTS`, `SECTION_PREFIX`, regex in `intents()` | |
| stopwords | `STOP` (also used by `check_answer`) | |

Workflow:
1. Reproduce with the full Wikivoyage standalone (`docker compose up -d --build`) and look at the
   `score` field in `/api/ask` responses. Regression questions: "What should I eat in Lisbon?",
   "what to eat in lisbon", "How do I get from the airport to the centre in Osaka?", "How do I get
   from Kansai airport to Osaka city centre?", "Is the tap water safe in Mexico City?".
2. Add a fixture-based test in `tests/test_guide.py`: either an article in the fake ZIM plus an
   `ask()` assertion, or a pure `score_passages` unit test with hand-built candidates like
   `test_place_names_still_count_outside_their_own_article`.
3. Watch for the known trade-off: boosting title overlap helped "Kansai airport" but hurt
   "Lisbon › Eat".
4. `STOP` changes also change `check_answer`'s content-word set (and so the reject rate): check
   `/api/stats` after a batch of questions.
5. If the change parses more or caches anything, re-measure memory (see [memory.md](memory.md)).

### Prompt / guard changes

- `SYSTEM`, `prompt()`, `schema()` in `ask.py`; sampling params and `ANSWER_TOKENS` in `app.py`
  `chat()`. Keep `response_format` json_schema: the parser expects `{answer, sources}`.
- Loosening `check_answer` (60% threshold, number rule) trades hallucination risk for accept
  rate; the page always labels the answer as model-written and shows the passages below it.
- `cache_prompt: true` benefits from a stable system prompt prefix; don't put per-request data
  before the passages.

### New endpoints or pages

- Relative URLs only (the app is at `/guide/`); put static assets in `static/` and reference
  them as `static/...` from the index or `{prefix}static/...` from article pages.
- Anything that renders ZIM content must go through `sanitize()` and carry `ARTICLE_CSP`.
- lxml-heavy work → `run_in_threadpool(_heavy, ...)`.
- If `update.sh` should wait on something new, it reads `/health`; keep `content_ready`'s meaning.
- Document new env vars in `app.yaml` (the platform reads it as the manifest) and the README.
