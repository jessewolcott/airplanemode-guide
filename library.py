"""The guide's content: Kiwix ZIM files read straight from disk with libzim.

Every *.zim in ZIM_DIR is opened at start-up (and again after a download finishes). Each
archive gets an id, its file name without ".zim", and its entries are served at
    z/<id>/<path>
so the relative links inside ZIM articles ("../Lisbon", "Lisbon/Alfama") resolve to other
articles in the same archive without any rewriting.

Articles (text/html) are never served as they are: they're cut down to the article body,
stripped of scripts, event handlers, forms, frames and inline styles blocks, and wrapped in
the app's own page with a Content-Security-Policy that forbids scripts.
"""
import ctypes
import html
import logging
import re
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import libzim.reader
import libzim.search
import libzim.suggestion
from lxml import etree
from lxml import html as lhtml

log = logging.getLogger("guide")

# Elements that never make it into an article page.
DROP_TAGS = {"script", "style", "link", "meta", "noscript", "iframe", "frame", "frameset", "object",
             "embed", "applet", "form", "input", "button", "select", "textarea", "base", "template",
             "audio", "video", "source", "track", "canvas", "dialog", "portal"}
# MediaWiki / mwoffliner chrome that's useless offline.
DROP_CLASSES = {"mw-editsection", "mw-jump-link", "printfooter", "catlinks", "mw-indicators",
                "noprint", "mw-kartographer-container", "mapframe", "navbox", "metadata",
                "mw-empty-elt"}
DROP_IDS = {"siteSub", "contentSub", "contentSub2", "jump-to-nav", "mw-navigation", "footer",
            "catlinks", "toc"}
URL_ATTRS = ("href", "src", "srcset", "action", "formaction", "poster", "background", "xlink:href",
             "data", "cite", "longdesc", "usemap")
SAFE_SCHEMES = {"", "http", "https", "geo", "tel", "mailto", "sms"}

# Raw (non-article) entries we're willing to serve, by mimetype prefix. Scripts are not:
# nothing on our pages runs them.
RAW_OK = ("text/css", "image/", "font/", "application/font", "application/vnd.ms-fontobject",
          "text/plain")

ARTICLE_CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
               "font-src 'self'; script-src 'none'; object-src 'none'; base-uri 'none'; "
               "form-action 'self'; frame-ancestors 'self'")
# Raw files (SVGs especially) could be opened as a document on the box's origin, which the
# admin panel shares: sandbox them so nothing in them can run as us.
RAW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


class NotReady(Exception):
    pass


# Parsing a big article with lxml takes ~10 MB for a moment. Two at a time at most, and
# hand the memory back to the system afterwards: glibc otherwise keeps it, and the app has
# a 256 MB limit on the box.
heavy = threading.BoundedSemaphore(2)
try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:  # not glibc (macOS, Windows, musl)
    _libc = None


def trim_memory():
    if _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass


class Zim:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem
        self.archive = libzim.reader.Archive(str(path))
        a = self.archive

        def meta(key):
            try:
                return bytes(a.get_metadata(key)).decode("utf-8", "replace")
            except Exception:
                return ""

        self.title = meta("Title") or self.id
        self.name = meta("Name") or self.id
        self.description = meta("Description")
        self.date = meta("Date")
        self.source = meta("Source")
        self.language = meta("Language")
        self.articles = a.article_count
        self.fulltext = a.has_fulltext_index
        self.lock = threading.Lock()  # libzim's search objects aren't shared across threads
        self._searcher = None

    def searcher(self):
        """One Xapian searcher per archive, reused (opening one costs memory each time)."""
        if self._searcher is None:
            self._searcher = libzim.search.Searcher(self.archive)
        return self._searcher

    def info(self):
        return {"id": self.id, "title": self.title, "name": self.name, "date": self.date,
                "articles": self.articles, "fulltext": self.fulltext, "source": self.source,
                "size": self.path.stat().st_size}


class Library:
    def __init__(self, zim_dir: Path, cluster_cache_mb: int = 8):
        self.zim_dir = zim_dir
        self.zims: dict[str, Zim] = {}
        self.lock = threading.Lock()
        try:
            # libzim keeps decompressed clusters in memory; bound it for small boxes
            libzim.reader.set_cluster_cache_max_size(cluster_cache_mb * 1024 * 1024)
        except Exception as e:  # older libzim
            log.warning("couldn't set the cluster cache size: %s", e)

    def reload(self):
        found = {}
        for p in sorted(self.zim_dir.glob("*.zim")):
            if p.stem in self.zims and self.zims[p.stem].path.stat().st_mtime == p.stat().st_mtime:
                found[p.stem] = self.zims[p.stem]
                continue
            try:
                found[p.stem] = Zim(p)
                log.info("opened %s (%d articles, full-text index: %s)", p.name, found[p.stem].articles,
                         found[p.stem].fulltext)
            except Exception as e:
                log.warning("can't open %s: %s", p.name, e)
        with self.lock:
            self.zims = found
        return list(found)

    @property
    def ready(self):
        return bool(self.zims)

    def get(self, zim_id) -> Zim | None:
        return self.zims.get(zim_id)

    def _all(self):
        if not self.zims:
            raise NotReady()
        return list(self.zims.values())

    # ---------------- search ----------------
    def suggest(self, q, limit=8):
        """Title suggestions (as you type). libzim folds a redirect and its target into one
        suggestion and may return the redirect ("Lisboa" for Lisbon), so redirects are
        resolved and shown under the target's title; an exact title match comes first."""
        out, seen = [], set()

        def add(z, e):
            if e.is_redirect:
                e = e.get_redirect_entry()
            if (z.id, e.path) not in seen:
                seen.add((z.id, e.path))
                out.append({"zim": z.id, "path": e.path, "title": e.title})

        exact = self.title_entry(q.strip())
        if exact:
            add(*exact)
        for z in self._all():
            if not z.archive.has_title_index:
                continue
            with z.lock:
                s = libzim.suggestion.SuggestionSearcher(z.archive).suggest(q)
                paths = list(s.get_results(0, limit))
            for p in paths:
                try:
                    add(z, z.archive.get_entry_by_path(p))
                except KeyError:
                    continue
        return out[:limit]

    def search(self, q, limit=20):
        """Full-text search with snippets (HTML, only <b> kept)."""
        out = []
        for z in self._all():
            if not z.fulltext:
                continue
            with z.lock:
                res = z.searcher().search(libzim.search.Query().set_query(q))
                total = res.get_estimated_matches()
                items = [(r.path, r.title, r.snippet) for r in res.get_results(0, limit)]
            for path, title, snippet in items:
                out.append({"zim": z.id, "path": path, "title": title or path.replace("_", " "),
                            "snippet": clean_snippet(snippet), "total": total})
        return out[:limit]

    def title_entry(self, title):
        """Exact (case-insensitive-ish) title lookup across archives: (zim, entry) or None."""
        variants = dict.fromkeys([title, title[:1].upper() + title[1:], title.title()])
        for z in self._all():
            for t in variants:
                try:
                    e = z.archive.get_entry_by_title(t)
                except KeyError:
                    continue
                if e.is_redirect:
                    e = e.get_redirect_entry()
                return z, e
        return None

    # ---------------- entries ----------------
    def entry(self, zim_id, path):
        z = self.get(zim_id)
        if not z:
            return None, None
        try:
            return z, z.archive.get_entry_by_path(path)
        except KeyError:
            return z, None

    def article_html(self, zim_id, path):
        """(Zim, title, raw article HTML) for an article, following redirects; or None."""
        z, e = self.entry(zim_id, path)
        if not e:
            return None
        if e.is_redirect:
            e = e.get_redirect_entry()
        item = e.get_item()
        if not item.mimetype.startswith("text/html"):
            return None
        return z, e.title, e.path, bytes(item.content).decode("utf-8", "replace")


def clean_snippet(snippet):
    """libzim snippets are HTML with <b> around matches. Keep only that."""
    if not snippet:
        return ""
    text = html.escape(html.unescape(re.sub(r"<(?!/?b>)[^>]*>", "", snippet)))
    return text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")


# ---------------- article pages ----------------
def article_root(doc):
    """The article body: mwoffliner's <main id="content">, else the <body>."""
    for xp in ("//main[@id='content']", "//*[@id='mw-content-text']", "//main", "//body"):
        found = doc.xpath(xp)
        if found:
            return found[0]
    return doc


def _has_class(el, classes):
    cls = el.get("class")
    return bool(cls) and not classes.isdisjoint(cls.split())


def sanitize(root, zim_root):
    """Remove anything that could run code or phone home. `zim_root` leads from the
    article's URL back to the root of its ZIM (for the rare absolute link)."""
    for el in list(root.iter()):
        if not isinstance(el.tag, str):  # comments, processing instructions
            if el.getparent() is not None:
                el.drop_tree()
            continue
        tag = el.tag.lower()
        if (tag in DROP_TAGS or el.get("id") in DROP_IDS or _has_class(el, DROP_CLASSES)) \
                and el is not root and el.getparent() is not None:
            el.drop_tree()
            continue
        if tag in ("svg", "math"):  # inline SVG can carry scripts; nopic ZIMs only use it for icons
            if el.getparent() is not None:
                el.drop_tree()
            continue
        for attr in list(el.attrib):
            a = attr.lower()
            if a.startswith("on") or a in ("style",) and "url(" in el.attrib[attr].lower():
                del el.attrib[attr]
                continue
            if a in URL_ATTRS:
                val = el.attrib[attr].strip()
                scheme = urlsplit(val).scheme.lower() if ":" in val.split("/")[0] else ""
                if a == "src" and tag == "img" and val.startswith("data:"):
                    continue  # handled below
                if scheme not in SAFE_SCHEMES or val.lower().startswith(("javascript:", "vbscript:", "data:")):
                    del el.attrib[attr]
                    continue
                if a == "href" and val.startswith("/") and not val.startswith("//"):
                    el.attrib[attr] = (zim_root or "./") + val.lstrip("/")
        if tag == "img":
            src = el.get("src", "")
            # "nopic" ZIMs replace pictures with transparent placeholders: drop them
            if not src or src.startswith("data:") or src.startswith(("http:", "https:", "//")):
                if el.getparent() is not None:
                    el.drop_tree()
                continue
        if tag == "a":
            href = el.get("href", "")
            if href.startswith(("http:", "https:", "//")):
                el.set("rel", "noopener noreferrer")
                el.set("target", "_blank")
                el.set("class", ((el.get("class") or "") + " ext").strip())
                el.set("title", "Needs the internet")
            elif href.startswith("geo:"):
                el.set("class", ((el.get("class") or "") + " geo").strip())
                el.set("title", "Open in a map app")
    return root


def render_article(zim: Zim, title, path, raw_html, prefix):
    """The app's own page around a sanitized article body."""
    doc = lhtml.document_fromstring(raw_html)
    root = sanitize(article_root(doc), prefix[: -len("../../")])
    body = etree.tostring(root, encoding="unicode", method="html")
    src = f"https://{zim.source}/wiki/{quote(path)}" if zim.source else ""
    esc = html.escape
    return f"""<!doctype html>
<html lang="{esc(zim.language[:2] or 'en')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{esc(title)} · Travel Guide</title>
<link rel="stylesheet" href="{prefix}static/article.css">
<link rel="icon" href="{prefix}static/icon.svg" type="image/svg+xml">
</head>
<body>
<header class="bar">
  <a class="home" href="{prefix}" title="Travel Guide home">&#8592; Guide</a>
  <form action="{prefix}" method="get" role="search"><input name="q" type="search" placeholder="Search the guide" aria-label="Search the guide"></form>
</header>
<article class="zim">
{body}
</article>
<footer class="attrib">From <b>{esc(zim.title)}</b>{(" (" + esc(zim.date) + ")") if zim.date else ""}, available under
<a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noopener noreferrer">CC BY-SA 4.0</a>.
{f'Online version: <a href="{esc(src)}" target="_blank" rel="noopener noreferrer">{esc(zim.source)}</a>.' if src else ""}
</footer>
</body>
</html>"""


def prefix_for(path):
    """Relative path from z/<id>/<path> back to the app root."""
    return "../" * (path.count("/") + 2)


def raw_allowed(mimetype):
    return mimetype.startswith(RAW_OK)


# ---------------- passages (for "ask") ----------------
BLOCKS = ("p", "li", "dd", "blockquote")
HEADINGS = ("h2", "h3", "h4")


def passages(raw_html, max_words=110):
    """Split an article into passages: (section heading, anchor id, text).
    Wikivoyage listings (li) and paragraphs become passages; short neighbours in the same
    section are merged, long ones split at sentence ends."""
    doc = lhtml.document_fromstring(raw_html)
    root = article_root(doc)
    for el in root.xpath(".//script|.//style|.//*[contains(concat(' ', normalize-space(@class), ' '), ' mw-editsection ')]"):
        if el.getparent() is not None:
            el.drop_tree()
    out = []
    h2 = h3 = ""
    anchor = ""
    buf, buf_head, buf_anchor = [], None, ""

    def flush():
        nonlocal buf
        if buf:
            out.append((buf_head, buf_anchor, " ".join(buf)))
        buf = []

    for el in root.iter(*HEADINGS, *BLOCKS):
        tag = el.tag
        if tag in HEADINGS:
            flush()
            text = " ".join(el.text_content().split())
            if tag == "h2":
                h2, h3 = text, ""
            else:
                h3 = text
            anchor = el.get("id") or anchor
            continue
        # skip blocks nested in another block (the outer one already has their text)
        if any(a.tag in BLOCKS for a in el.iterancestors()):
            continue
        text = " ".join(el.text_content().split())
        if len(text) < 30:
            continue
        head = f"{h2} › {h3}" if h3 else h2
        words = text.split()
        if buf and (head != buf_head or len(" ".join(buf).split()) + len(words) > max_words):
            flush()
        if not buf:
            buf_head, buf_anchor = head, anchor
        if len(words) <= max_words:
            buf.append(text)
            continue
        # long block: split into sentence groups
        flush()
        buf_head, buf_anchor = head, anchor
        chunk = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if chunk and len(" ".join(chunk).split()) + len(sentence.split()) > max_words:
                out.append((head, anchor, " ".join(chunk)))
                chunk = []
            chunk.append(sentence)
        if chunk:
            out.append((head, anchor, " ".join(chunk)))
    flush()
    return out
