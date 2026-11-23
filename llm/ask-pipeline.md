# "Ask" end to end (`ask.py`, `app.py` ask/answer)

Design: everything before the model is **deterministic, fast (~0.1 s on a PC) and useful on its
own**. The page shows the passages immediately; the model's answer, if any, arrives later on top
and must pass a deterministic grounding check.

```
POST api/ask {question}
  └─ _heavy(gather)                                  (thread pool, heavy semaphore, malloc_trim)
       ├─ find_articles   place names → title_entry; full-text search; ≤ 3 articles
       ├─ article_html + passages()  per article → ~110-word passages with section + anchor
       ├─ dedupe (first 160 chars)
       ├─ score_passages  BM25 + section-intent boost + place boost
       └─ select          ≤ 6 passages, ≤ PASSAGE_TOKENS (900)
  └─ store {question, passages} under a uuid in `asks` (≤ 100)
  └─ return {id, articles, passages, model}
POST api/answer {id}
  └─ cached? → return.  no LLM_URL → none.  model_turn within LLM_WAIT? else busy
  └─ chat(prompt(question, passages), 220 tokens, schema(n))  → JSON
  └─ check_answer() → ok (cached)  |  rejected  |  timeout  |  offline
```

## 0. Text helpers (ask.py:18-85)

- `STOP` (18-28): English stopwords plus travel-question filler (`best good recommend way thing
  around also really much many visit visiting`...).
- `WORD_RE = [\w][\w'’-]*` (Unicode): words may contain apostrophes and hyphens
  (`street-food`, `Shin-Osaka`).
- `stem(w)`: lower-case, strip `'’-` at ends; for words longer than 4 chars, strip `ies`→`y`,
  `es`, or `s` (not `ss`). Crude on purpose (`dishes`→`dish`, `cities`→`city`, `glasses` kept).
- `keywords(q)`: stemmed words not in `STOP`, length > 1.

## 1. Find articles: `find_articles(lib, question, limit=3)` (108-137)

Returns `[(zim_id, path, title, is_place)]`, max `MAX_ARTICLES = 3`, deduped by `(zim, path)`.

1. **Named places** — `place_candidates(question)` (88-100): all word n-grams for n = 4, 3, 2, 1
   (longest first) whose first **and** last words start with a capital letter. Single words are
   dropped if they're stopwords (`Is`, `I`, ...). Order-preserving dedupe. E.g.
   `"Is the tap water safe in Mexico City?"` → `["Mexico City", "Mexico", "City"]`; a lowercase
   question gives `[]`. Each candidate goes through `lib.title_entry()` (exact title, first letter
   upper, Title Case); hits are added as places. Stops after **2** articles have been picked.
2. **Full-text search** — query = the question's non-stopword words joined with spaces (or the
   whole question if none); `lib.search(query, limit=10)`; exceptions swallowed (`[]`).
3. **Lowercase places** — any hit whose title (parenthetical removed, lower-cased) appears as a
   whole word sequence in the lower-cased question (`_title_in_question`, 103-105) and whose path
   has no `/` (not a district subpage) is added as a place. This catches
   `"what to eat in lisbon"`.
4. Remaining hits are added in full-text order as non-places until 3 articles.

`articles` in the API response is this list (in this order), shown on the page as "Read more".

## 2. Split into passages: `library.passages()` (library.py:357-415)

`passages(raw_html, max_words=110)` → `[(section_heading, anchor_id, text)]`:

- Parse, take `article_root`, drop `script`, `style` and `.mw-editsection` elements.
- Iterate `h2 h3 h4 p li dd blockquote` in document order:
  - **Heading**: flush the buffer. `h2` sets the main heading and clears the sub-heading;
    `h3`/`h4` set the sub-heading (an `h4` replaces the `h3`). `anchor = heading id or the
    previous anchor`. Heading text is whitespace-normalised.
  - **Block**: skipped if nested inside another block (`li` in `li`, `p` in `blockquote`, ...)
    since the outer block's `text_content()` already has it; skipped if shorter than 30 chars.
    Section label = `"H2 › H3"` or `"H2"` (`""` for the intro before any heading).
  - **Merging**: short blocks accumulate in a buffer while they're in the same section and the
    total stays ≤ `max_words`; otherwise the buffer is flushed first.
  - **Long blocks** (> `max_words`): flushed separately, split at sentence ends
    (`(?<=[.!?])\s+`) into chunks of ≤ `max_words` (a single overlong sentence stays whole).
- So a Wikivoyage listing (`li`) or paragraph is roughly one passage; short listings get merged.

In `gather` each passage becomes a candidate dict
`{zim, path (real, after redirect), title, section, anchor, text, place}`. Candidates whose first
160 chars were already seen (across all articles) are dropped: region articles repeat each
other's itineraries.

## 3. Rank: `score_passages(cands, question, k1=1.4, b=0.75, places=())` (150-191)

`places` = titles of the articles picked as places. Per candidate:

1. **Terms** = set of question keywords. If the candidate is from a place article
   (`c["place"]` and its title is in `places`) and the question has keywords beyond that title's
   own words, the title's words are **removed** from the terms for this passage. Rationale: the
   place name is in every passage of its own article, so it only selects the article. In *other*
   articles it still counts — "Kansai airport" must favour the Kansai section of the Osaka
   article over Osaka's Itami section (see the test below).
2. **BM25** over the candidate set (stemmed word counts per passage, average length over all
   candidates, `df` only for query terms):
   `idf = ln(1 + (n - df + 0.5) / (df + 0.5))`,
   `s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len / avg))`.
3. **Section intent** — `intents(question)` (76-85) maps question words (raw or stemmed) to
   Wikivoyage's standard sections via `INTENTS` (31-49):
   `eat, drink, sleep, get in, get around, see, do, buy, stay safe, stay healthy, connect, talk,
   respect, understand`. Plus a regex: `get to|from|into` or `from the airport` → `get in`.
   If any intent was found: matching section → `s += 2.5`, otherwise `s *= 0.6`.
   `section_matches(head, wanted)` (140-147): the lower-cased label starts with the section name,
   or contains `"› <section>"`, or (for `get in` / `get around`) starts with / contains
   `"› <prefix>"` for the `SECTION_PREFIX` lists (`by plane`, `by train`, `arriv`, `by metro`,
   `by taxi`, `by foot`, ...).
4. **Place boost**: `s *= 1.6` for passages from place articles (applied after the intent bonus,
   so it multiplies that too).
5. **Short passage penalty**: fewer than 15 words → `s *= 0.7`.

Sorted by score descending. Example intents: "What should I eat in Lisbon?" → `["eat"]`;
"How do I get from the airport to the centre in Osaka?" → `["get in", "get around"]` (airport →
get in, centre → get around).

## 4. Select within budget: `gather(lib, question, budget_tokens=900, max_passages=6)` (198-226)

Walk the scored list:
- stop at the first score ≤ 0 or once 6 passages are taken;
- `cost = tokens(text) + 12` where `tokens = len(text)//4 + 1` (rough estimate for small-model tokenizers; the 12
  covers the `[n] (Title — Section)` label);
- if it would exceed the budget: skip it when something is already taken (a smaller one further
  down may fit); if it's the very first, trim it to `budget*4` chars at a word boundary + `…`;
- each kept passage gets `score` (rounded to 2 places).

`app.py` passes `PASSAGE_TOKENS` as the budget. Result: `{"articles": [...], "passages": [...]}`.

## 5. The `/api/ask` endpoint (`app.py:152-164`)

- `Ask.question`: 3–300 chars (pydantic; violations → 422 with a list `detail`).
- 503 via `not_ready()` if no ZIM is open.
- `STATS["asks"] += 1`; `gather` runs via `run_in_threadpool(_heavy, ...)`.
- With passages: a `uuid4().hex` id; `asks[id] = {question, passages}`; oldest entries evicted
  beyond 100 (insertion order — reading doesn't refresh an entry).
- Response `model` = `bool(LLM_URL) and bool(passages)`: tells the page whether to call
  `/api/answer`.

## 6. The model's half

### Prompt (`ask.prompt`, 254-261)

```
system: SYSTEM (237-243): travel guide assistant on an offline router; answer using ONLY the
        numbered passages; practical and brief, 2–5 sentences; keep names, prices and times
        exactly; if not covered, say the guide doesn't cover it; reply in JSON: "answer" (no
        passage numbers), "sources" (numbers used).
user:   Passages:
        [1] (Lisbon — Eat) Lisbon is famous for pastel de nata ...

        [2] (Lisbon — Eat) Cervejaria Ramiro, ...

        Question: <question>
        Answer in 2 to 5 full sentences, from the passages only.
```

`(Title — Section)`: the section part is omitted for intro passages. Typical prompt ≈ 850
tokens.

### Schema (`ask.schema(n)`, 246-251) — enforced by llama.cpp's grammar

```json
{"type": "object",
 "properties": {"answer": {"type": "string", "minLength": 60, "maxLength": 900},
                "sources": {"type": "array", "maxItems": 4,
                            "items": {"type": "integer", "minimum": 1, "maximum": n}}},
 "required": ["answer", "sources"]}
```

### The call (`app.py:167-175`)

`POST {LLM_URL}/v1/chat/completions` with `messages`, `max_tokens = ANSWER_TOKENS (220)`,
`temperature 0.2`, `repeat_penalty 1.1`, `cache_prompt: true` (llama.cpp reuses the KV cache for
a repeated prefix — the system prompt), `response_format: {"type": "json_schema", "json_schema":
{"name": "answer", "schema": ...}}`, `timeout=LLM_TIMEOUT`. Returns
`choices[0].message.content.strip()`.

### Concurrency

`model_turn` (`asyncio.Semaphore(1)`): one model call at a time from this app. Waiting longer
than `LLM_WAIT` (3 s) → `busy` (the page says so and keeps the passages). The standalone
llama.cpp runs with `-np 2` (2 parallel slots); on the box the model is shared with other apps.

### `check_answer(answer, sources, picked)` (264-283) — the grounding guard

1. Strip citation markers like `[1]` / `[1, 2]` from the answer; `< 15` chars → `ValueError("empty")`.
2. **Numbers**: source text = all passages' text + title + section. With commas removed (so
   `1,000` → `1000`), every number in the answer (`\d+(?:[.,:]\d+)*`, e.g. `1.85`, `10:30`) must
   appear among the source's numbers, except single-digit numbers ("2 lines").
   Else `ValueError("number not in passages")`.
3. **Content words**: answer words not in `STOP` and longer than 3 chars, stemmed; at least 60%
   must appear among the stemmed source words. Else `ValueError("not grounded")`.
4. **Sources**: keep ints in `1..len(picked)`, dedupe, sort; default `[1]` if none survive.
5. Returns `(clean_answer, sources)`.

What it can't catch: an answer built from words that are in the passages but combined wrongly,
e.g. describing Itami's monorail for a Kansai question (see gotchas).

### Outcome handling (`app.py:187-219`)

- `ValueError` (includes `json.JSONDecodeError`, a non-dict JSON, and `check_answer`'s reasons)
  → `rejected`; `rejected_by[reason]` where reason ∈ `empty`, `number not in passages`,
  `not grounded`, else `bad output`; the raw output (≤300 chars) is logged at INFO.
- `httpx.TimeoutException` / `asyncio.TimeoutError` → `timeout`.
- any other exception → `offline` (connection refused, `raise_for_status`, missing keys).
- `model_turn` is always released (`finally`).
- Only `ok` answers are cached on the ask entry (`a["answer"]`); calling `/api/answer` again for
  a rejected/timeout id runs the model again.
- `STATS["answers"][status] += 1` for ok/rejected/timeout/offline/busy (not `none`).

## Fallbacks when there's no model

| Situation | `/api/ask` | `/api/answer` | Page |
|---|---|---|---|
| `LLM_URL=""` | passages, `model: false` | `{status: "none"}` | shows passages only, never calls answer |
| `/health` says `llm: "none"` | — | — | page skips answer even if `model` true |
| model down | passages, `model: true` | `offline` | note "isn't available right now" + passages |
| model slow | | `timeout` after 90 s | note + passages |
| model busy | | `busy` after 3 s | note + passages |
| answer fails guard | | `rejected` | "didn't stick to the guide, so here's the guide itself" |
| no passages | `id: null, passages: [], model: false` | — | "The guide doesn't seem to cover that..." |

## Tests that pin this behaviour

`test_passages`, `test_intents_and_places`, `test_ask_finds_the_right_passages` (Lisbon → Eat,
lowercase lisbon → Eat, Osaka airport → Get in, budget respected, nonsense → no passages),
`test_place_names_still_count_outside_their_own_article` (Kansai vs Itami unit test on
`score_passages`), `test_answer_from_model` (citations stripped, out-of-range source dropped,
request body has `cache_prompt` and `json_schema`, answers cached), `test_answer_guards`
(invented number, ungrounded answer, connect error → offline, read timeout → timeout, unknown id
→ 404, stats reasons), `test_answer_busy`, `test_no_model`.
