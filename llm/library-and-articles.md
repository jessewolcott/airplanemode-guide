# Library and articles (`library.py`, `app.py` article route)

## ZIM basics

A Kiwix ZIM is a single compressed archive of entries addressed by **path** (`Lisbon`,
`Lisbon/Alfama`, `_res_/style.css`, ...). Entries can be redirects. Each has a title and a
mimetype. Content is stored in compressed **clusters**; libzim decompresses whole clusters and
caches them. A ZIM may carry a title index (suggestions) and a Xapian full-text index. The
default Wikivoyage `nopic` ZIM has both and has pictures replaced by placeholders.

Python binding: `libzim==4.1.0` (`libzim.reader.Archive`, `libzim.search.Searcher/Query`,
`libzim.suggestion.SuggestionSearcher`). It needs Python 3.11+.

## `Zim` (library.py:78-111)

Wraps one archive. On construction it opens `libzim.reader.Archive(str(path))` and reads
metadata (decoded UTF-8 with `replace`, empty string on error): `Title` (fallback id), `Name`
(fallback id), `Description`, `Date`, `Source` (e.g. `en.wikivoyage.org`), `Language`
(e.g. `eng`). Also `articles = archive.article_count`, `fulltext = archive.has_fulltext_index`.

- `id = path.stem` (file name without `.zim`).
- `lock`: a `threading.Lock` held around search/suggestion calls.
- `searcher()`: one `libzim.search.Searcher` per archive, created lazily and reused (each one
  costs memory to open).
- `info()`: `{id, title, name, date, articles, fulltext, source, size}` (size = file size),
  used in `/health`.

## `Library` (library.py:114-232)

- `__init__(zim_dir, cluster_cache_mb=8)`: calls
  `libzim.reader.set_cluster_cache_max_size(mb * 1024 * 1024)` (process-wide; a warning is logged
  on older libzim that lacks it).
- `reload()` (125-139): globs `zim_dir/*.zim` in sorted order. An already-open `Zim` is reused
  if its file's mtime is unchanged; otherwise a new `Zim` is opened (failures are logged and the
  file is skipped). Then `self.zims` is replaced in one assignment under `self.lock`. Files that
  disappeared are simply dropped (their archives close when garbage-collected). Returns the ids.
  Called at startup (thread pool) and by the downloader after a file lands (event loop, sync).
- `ready` property: `bool(self.zims)`. This is `/health`'s `content_ready`.
- `_all()`: list of Zims, or raises `NotReady` (mapped to a 503 by the endpoints).
- `entry(zim_id, path)` → `(zim|None, entry|None)`; `KeyError` from libzim → entry None.
- `article_html(zim_id, path)` (222-232): follows one redirect, returns
  `(zim, title, real_path, html_str)` for `text/html` entries, else `None`. (The docstring says
  a 3-tuple; it's a 4-tuple.) Content decoded UTF-8 with `replace`.

### Title lookup: `title_entry(title)` (198-210)

"Case-insensitive-ish": tries the exact string, first-letter-uppercased, and `str.title()`
(deduped, in that order) with `archive.get_entry_by_title` in each ZIM (sorted file order); the
first hit wins, redirect resolved. Used for exact-match suggestions and for place lookup in ask.
`"lisbon"` → tries `lisbon`, `Lisbon` → hit. `"rio de janeiro"` → `.title()` gives
`Rio De Janeiro`, which won't match `Rio de Janeiro` — lowercase multi-word names rely on the
full-text path in ask instead.

### Suggestions: `suggest(q, limit=8)` (154-181)

1. Exact title (`title_entry(q.strip())`) first, if any.
2. For each ZIM with `has_title_index`: under `z.lock`, a fresh `SuggestionSearcher(archive)
   .suggest(q)` and `get_results(0, limit)` (paths). Each path is resolved; redirects are shown as
   their target (libzim may return the redirect, e.g. `Lisboa` for Lisbon).
3. Deduped on `(zim id, path)`; cut to `limit`.

### Full-text search: `search(q, limit=20)` (183-196)

For each ZIM with `fulltext`: under `z.lock`, `z.searcher().search(Query().set_query(q))`,
`get_estimated_matches()` as `total`, then `get_results(0, limit)` → `(path, title, snippet)`.
Result dicts: `{zim, path, title (fallback: path with _→space), snippet: clean_snippet(...),
total}`. Results are concatenated across ZIMs in file order and cut to `limit` (so a second ZIM
only shows if the first had fewer than `limit` hits). `/api/search` asks for 25; ask uses 10.
ZIMs without a full-text index are skipped silently.

`clean_snippet()` (235-240): removes every tag except `<b>`/`</b>`, unescapes, re-escapes
everything, then restores `<b>`/`</b>`. The page injects snippets as HTML, so this function is
the XSS boundary for search results.

## Article URLs: `z/<zim id>/<path>`

Entries are served under `z/<id>/` so a ZIM's own relative links work unchanged: from
`z/wv/Lisbon`, `href="Porto"` resolves to `z/wv/Porto`; from `z/wv/Lisbon/Alfama`,
`href="../Lisbon"` resolves to `z/wv/Lisbon`. No link rewriting is needed except for the rare
root-absolute `href="/X"`.

`prefix_for(path)` (343-345) = `"../" * (path.count("/") + 2)`: the relative path from
`z/<id>/<path>` back to the app root (`Lisbon` → `../../`, `Lisbon/Alfama` → `../../../`).
The page's stylesheet, icon, home link and search form use it.

### The route (`app.py:235-257`)

```
z, e = library.entry(zim_id, path)
no zim     -> 503 not_ready() if the library is empty, else 404 "No such guide."
no entry   -> 404 "That page isn't in the guide."
redirect   -> 302 Location: "../" * path.count("/") + quote(target_path)
text/html  -> run_in_threadpool(_heavy, _render, ...) -> HTMLResponse with
              CSP: ARTICLE_CSP, X-Content-Type-Options: nosniff,
              Referrer-Policy: no-referrer, Cache-Control: private, max-age=3600
raw_allowed(mimetype) -> Response(bytes(item.content)) with
              CSP: RAW_CSP, nosniff, Cache-Control: public, max-age=86400
otherwise  -> 404 "Not served."
```

The redirect Location is relative to the current entry's directory: `z/id/Lisboa` → `Lisbon`
(test asserts exactly `"Lisbon"`). `quote()` keeps `/`.

`_render(z, zim_id, path)` (`app.py:231-233`) calls `library.article_html` (which re-fetches the
entry) and `render_article(z, title, real_path, raw, prefix_for(path))`.

## `sanitize(root, zim_root)` (library.py:258-307)

Input: the lxml element returned by `article_root(doc)` — the first of
`//main[@id='content']` (mwoffliner), `//*[@id='mw-content-text']`, `//main`, `//body`, else the
document. Everything outside (head, nav, footer chrome) is gone already.

Walks `list(root.iter())` and for each node:

1. **Comments / processing instructions** (non-string tag): dropped.
2. **Dropped elements** (with their subtree), unless it's the root:
   - `DROP_TAGS` (30-32): `script style link meta noscript iframe frame frameset object embed
     applet form input button select textarea base template audio video source track canvas
     dialog portal`.
   - id in `DROP_IDS` (37-38): `siteSub contentSub contentSub2 jump-to-nav mw-navigation footer
     catlinks toc`.
   - any class in `DROP_CLASSES` (34-36): `mw-editsection mw-jump-link printfooter catlinks
     mw-indicators noprint mw-kartographer-container mapframe navbox metadata mw-empty-elt`.
3. **`svg` and `math`** dropped (inline SVG can carry scripts; nopic ZIMs use it only for icons).
4. **Attributes**:
   - any `on*` attribute deleted; `style` deleted **only if it contains `url(`** (operator
     precedence: `a.startswith("on") or (a == "style" and "url(" in value)`). Other inline styles
     stay; `article.css` neutralises `[style*=background]` / `[style*=color]`.
   - URL attributes (`URL_ATTRS`: `href src srcset action formaction poster background xlink:href
     data cite longdesc usemap`): the scheme is taken only if the first `/`-segment contains a
     `:`. If the scheme isn't in `SAFE_SCHEMES` (`"" http https geo tel mailto sms`) or the value
     starts with `javascript:`, `vbscript:` or `data:`, the attribute is deleted. Exception:
     `img src="data:..."` is skipped here and handled in step 5.
   - Root-absolute `href="/X"` (not `//`) is rewritten to `zim_root + "X"` (`zim_root` or
     `./` when empty) so it points at the ZIM's root.
5. **`img`**: dropped if `src` is empty, `data:`, `http:`, `https:` or `//` (nopic placeholders
   and anything remote). Relative images inside the ZIM stay (served as raw files).
6. **`a`**:
   - external (`http:`, `https:`, `//`): `rel="noopener noreferrer"`, `target="_blank"`, class
     `ext` appended, `title="Needs the internet"` (CSS adds ↗).
   - `geo:`: class `geo` appended, `title="Open in a map app"` (CSS adds ⌖).
   - `tel:`/`mailto:`/`sms:` are kept as is (tappable).

`zim_root` passed by `render_article` is `prefix[: -len("../../")]`, i.e. `"../" *
path.count("/")`: from `Lisbon` it's `""` (→ `./`), from `Lisbon/Alfama` it's `../`.

## The article page: `render_article(zim, title, path, raw_html, prefix)` (310-340)

1. `lhtml.document_fromstring(raw_html)` → `article_root` → `sanitize` →
   `etree.tostring(root, encoding="unicode", method="html")`.
2. Wraps it in the app's own HTML (all values `html.escape`d):
   - `<html lang>` from the ZIM's `Language` (first 2 chars, default `en`); `color-scheme: dark`.
   - `<title>{title} · Travel Guide</title>`, stylesheet `{prefix}static/article.css`, icon.
   - `<header class="bar">`: `← Guide` home link (`href="{prefix}"`), a GET search form
     (`action="{prefix}"`, input `name="q"`; the index page reads `?q=` and runs a search), and
     the AirplaneMode home button `a.am-home` (`href="{prefix}../"`, house icon; platform app
     contract item 10). The tests check its depth on a page and a subpage.
   - `<article class="zim">{body}</article>`
   - `<footer class="attrib">`: "From **{zim title}** ({date}), available under CC BY-SA 4.0."
     plus "Online version: {source}" linking to `https://{source}/wiki/{quote(path)}` when the
     ZIM has a `Source` (this assumes a MediaWiki `/wiki/` layout).

## CSPs

```
ARTICLE_CSP = default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;
              font-src 'self'; script-src 'none'; object-src 'none'; base-uri 'none';
              form-action 'self'; frame-ancestors 'self'
RAW_CSP     = default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox
```

Why both layers (sanitize **and** CSP): the app shares an origin with the admin panel on the
box. A script running on an article page could call the admin API with the user's cookies.
Sanitising removes active content; the CSP is the backstop if something slips through.
`RAW_CSP`'s `sandbox` matters for SVG images (`image/` includes `image/svg+xml`): an SVG opened
directly as a document would otherwise run its scripts on our origin.

## `RAW_OK` raw files (library.py:45-46)

`raw_allowed(mt)` = `mt.startswith(RAW_OK)` with
`RAW_OK = ("text/css", "image/", "font/", "application/font", "application/vnd.ms-fontobject",
"text/plain")`. Notably **not** JavaScript (`application/javascript` → 404; tested) and not
`application/json`/PDF/etc. Note that the article page itself only loads the app's
`article.css` — the ZIM's own CSS (`<link>`) is dropped by sanitize — so raw CSS is only fetched
if something requests it directly; images referenced from articles are the main raw traffic
(rare in `nopic`).

## Passages (library.py:352-415)

`passages(raw_html, max_words=110)` is the splitter used by ask; documented in
[ask-pipeline.md](ask-pipeline.md#2-split-into-passages-librarypassages). It parses the article
again independently of `render_article` (with its own, smaller cleanup: script, style,
`mw-editsection`).
