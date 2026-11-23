# The page (`static/index.html`) and the article view

Both pages are dark, phone-first, and self-contained: no external fonts, scripts or images
(the test `test_index_page_uses_relative_urls` asserts no `http://`/`https://` and no
root-absolute `fetch("/`, `href="/`, `src="/`, `action="/` in the index page). Palette tokens are
duplicated between `index.html`'s inline `<style>` and `article.css` (`--bg #0d0f12`,
`--accent #5ec4b6`, ...), matching the platform.

## `static/index.html` (one file, inline CSS + vanilla JS)

Served by `GET /` with `Cache-Control: no-cache`; it always loads, even with no content.

### Markup (79-120)

- Header: compass icon (inline SVG), "Travel **Guide**", and at the right end the AirplaneMode
  home button `a.am-home` (`href="../"`, 44 px house icon, accent focus ring; platform app
  contract item 10). It replaced the old "AirplaneMode" text link. No confirm on leaving: there's
  nothing to lose. `test_index_page_uses_relative_urls` checks it's there.
- `#banner` (`aria-live="polite"`): download / no-content status with a progress meter.
- Mode switch: two buttons `#modeAsk` / `#modeSearch` with `aria-pressed`.
- `form#form.box`: `input#q` (`maxlength=300`), button `#go`, suggestions `ul#sugg`.
- `#examples`: four example questions (Lisbon food, Osaka airport → centre, Mexico City tap
  water, Cape Town hiking). Visible only in Ask mode; clicking one fills the box and submits.
- `#out`: results area. Footer with `#credit` (replaced with the ZIM titles/dates from /health).

### Script (121-264)

Helpers: `$` = querySelector; `esc()` HTML-escapes; `art(zim, path, anchor)` builds the
relative article URL `z/<enc zim>/<each path segment encoded>#<enc anchor>`.
State: `mode` (`ask`), `ready`, `llm`, `run` (a counter to discard stale responses).

- **Mode** (`setMode`, 120-129): updates pressed state, button text, placeholder, aria-label,
  examples visibility; hides suggestions.
- **Health / banner** (143-172): `fetch("health")` once on load and then every 3 s **while not
  ready** (once ready, polling stops, so `llm` isn't refreshed later). Sets `ready`, `llm`, the
  credit line. When not ready, the banner shows by downloader state:
  `downloading` ("Downloading the guide… N%", "X MB of Y MB, about N min left. It only needs the
  internet this once.", meter), `verifying` ("Checking the download…"), `waiting` ("hasn't been
  downloaded yet… next time the box is online (last try: <error>)"), otherwise "No guide
  content on this box" with a hint about `.zim` / `ZIM_URL`.
  After the first health call, `?q=` in the URL (from an article's search box) switches to
  Search mode and runs it.
- **Suggestions** (174-200): only in Search mode, ≥ 2 chars, content ready; 150 ms debounce;
  `sRun` counter ignores stale replies; `GET api/suggest?q=`; results are links straight to the
  article. Arrow keys move between the input and the list; Escape or clicking outside hides it.
- **Submit** (203-219): bumps `run`, disables `#go`, calls `doAsk` or `doSearch`; errors are
  shown in `#out` (escaped). `post()` throws with the server's `detail` when it's a string, else
  "Something went wrong. Try again." (so a 422 for a too-short question gives the generic text).
- **Search** (`doSearch`, 214-225): `GET api/search?q=`; heading "Top N of about T results"
  (T = first result's `total`) or "N results"; cards link to articles with title and **snippet
  injected as HTML** (trusted because `clean_snippet()` keeps only `<b>`). Empty: "Nothing found
  for “q”. Try a place name."
- **Ask** (`doAsk`, 232-256):
  1. "Looking it up…" → `POST api/ask`.
  2. No passages → "The guide doesn't seem to cover that. Try naming the place, or use Search."
  3. Otherwise render `<div id="ans">`, then "Here's what the guide says" with one card per
     passage (number badge, **title** · section, escaped text; links to the article at the
     passage's anchor), then "Read more" with one card per article.
  4. If `d.model && llm !== "none"`: a waiting box ("The on-board model is writing a short answer
     from these passages. On the box that takes up to a minute…"), then `POST api/answer`.
     - `ok`: green answer box with the escaped answer and "From <source links> · written by the
       on-board model, so check the passages below." Sources map 1-based numbers to passages.
     - anything else (or a network error): a grey note with the server's `detail` + "The passages
       below are straight from the guide."
  The `run` check after each await drops results from a superseded submit.

Accessibility touches: `aria-pressed` mode buttons, `role="listbox"` suggestions,
`aria-live` banner, `prefers-reduced-motion` disables the dots animation and meter transition.

## The article view

HTML built server-side by `render_article()` (see
[library-and-articles.md](library-and-articles.md#the-article-page-render_articlezim-title-path-raw_html-prefix));
no JavaScript at all (CSP `script-src 'none'`).

`static/article.css`:
- Sticky `header.bar` with the "← Guide" link, a full-width search input (submits `?q=` to
  the index page) and the AirplaneMode home button (`a.am-home`, 44 px) at the right end.
  `[id] { scroll-margin-top: 72px }` so `#anchor` links from passage cards land below the bar.
- `article.zim`: 46rem column, 17px/1.62 text, accent links; `a.ext` dimmed with " ↗";
  `a.geo` prefixed with "⌖ ".
- Tables become horizontally scrollable blocks; template colours are overridden
  (`[style*="background"]` → transparent, `[style*="color"]` → inherit, infobox/warningbox/
  `[class*="box"]` → panel colours).
- Wikivoyage listings: name bold; address/phone/hours/price dimmed; listing metadata,
  coordinates, last-edit and empty map links hidden. Page banners and TOCs hidden.
- `footer.attrib`: licence/attribution line.

Screenshots of all three views are in `docs/screenshots/` (taken from the dev stack; retake when
the UI changes).
