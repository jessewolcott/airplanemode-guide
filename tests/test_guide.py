"""Travel Guide tests. Builds a tiny guide (a few fake travel articles) as a real ZIM with a
full-text index, so nothing is downloaded. The model and download servers are stubbed with
httpx.MockTransport.

Run:  python tests/test_guide.py        (Python 3.12 + requirements.txt; libzim needs 3.11+)
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="guide-test-"))
ZIM_DIR = TMP / "zim"
os.environ.update(ZIM_DIR=str(ZIM_DIR), ZIM_URL="", LLM_URL="http://llm.test", LLM_TIMEOUT="5",
                  LLM_WAIT="0.2")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402

ZIM_ID = "testvoyage_en_all_2026-01"


def page(title, body):
    """Shaped like mwoffliner's output: chrome, <main id="content">, sections, scripts."""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{title}</title>
<script>document.documentElement.className = "js"</script>
<link rel="stylesheet" href="./_res_/style.css"><script src="./_res_/evil.js"></script></head>
<body><div class="mw-page-container"><nav id="mw-navigation">Menu stuff</nav>
<main id="content" class="mw-body"><h1 id="firstHeading">{title}</h1>{body}</main>
<footer id="footer">Footer stuff</footer></div></body></html>"""


LISBON = page("Lisbon", """
<section><p><b>Lisbon</b> is the hilly capital of <a href="Portugal" rel="mw:WikiLink">Portugal</a>,
on the Tagus river, with trams, viewpoints and fado music. See also <a href="Porto">Porto</a>.</p>
<p>District guides: <a href="Lisbon/Alfama">Alfama</a>, and the <a href="/Porto">absolute link</a>.</p></section>
<section><h2 id="Get_in">Get in<span class="mw-editsection">[edit]</span></h2>
<h3 id="By_plane">By plane</h3>
<p>Humberto Delgado Airport is 7 km north of the centre. The red metro line runs from the airport
to Saldanha in about 20 minutes; a single ticket costs 1.85 euro with a Navegante card.</p>
<p onclick="alert(1)">Taxis to the centre cost about 15 euro. <a href="javascript:alert(1)">Don't click</a>.</p>
</section>
<section><h2 id="Eat">Eat</h2>
<p>Lisbon is famous for pastel de nata, a custard tart best eaten warm with cinnamon at Pasteis de
Belem. Grilled sardines are the classic food of the June festivals, and bacalhau, salt cod, is on
every menu. Time Out Market in Cais do Sodre gathers many of the city's best restaurants.</p>
<ul><li><span class="listing-name">Cervejaria Ramiro</span>, Avenida Almirante Reis 1. Famous seafood
restaurant: garlic prawns, clams and a steak sandwich to finish. <a href="geo:38.72,-9.13">map</a>
<a href="https://www.cervejariaramiro.com/">website</a></li></ul>
<iframe src="https://evil.example/"></iframe><style>body{background:red}</style>
<img src="data:image/svg+xml;charset=UTF-8,%3Csvg%3E%3C/svg%3E" alt="placeholder">
<svg onload="alert(1)"><script>alert(1)</script></svg>
</section>
<section><h2 id="Drink">Drink</h2>
<p>Bairro Alto fills with people drinking cheap beer in the street on weekend nights. Ginjinha, a
sour cherry liqueur, is served in tiny bars near Rossio square.</p></section>
<section><h2 id="Sleep">Sleep</h2>
<p>Hostels in Baixa and Chiado are among the best rated in Europe, from 25 euro a night in a dorm.</p>
</section>
<script>alert("inline")</script>""")

ALFAMA = page("Lisbon/Alfama", """
<section><p><b>Alfama</b> is the oldest district of <a href="../Lisbon">Lisbon</a>, a maze of steep
lanes below the castle. Back to <a href="../Portugal">Portugal</a>.</p></section>""")

PORTO = page("Porto", """
<section><p>Porto is the second city of Portugal, home of port wine.</p></section>
<section><h2 id="Eat">Eat</h2><p>The francesinha is Porto's famous sandwich: bread, steak, ham and
sausage, covered in melted cheese and a spicy beer sauce. Try it at Cafe Santiago.</p></section>""")

OSAKA = page("Osaka", """
<section><p>Osaka is Japan's third largest city, known for food and nightlife.</p></section>
<section><h2 id="Get_in">Get in</h2><h3 id="By_plane">By plane</h3>
<p>Kansai International Airport (KIX) is on an island in Osaka Bay. The Nankai Rapi:t train runs
from the airport to Namba in the city centre in 38 minutes; the JR Haruka express goes to
Tennoji and Shin-Osaka. Itami Airport handles domestic flights, with buses to Umeda.</p></section>
<section><h2 id="Eat">Eat</h2><p>Takoyaki, octopus balls, and okonomiyaki are Osaka's specialties;
Dotonbori is lined with stalls selling them.</p></section>""")

PORTUGAL = page("Portugal", """
<section><p>Portugal is a country in southern Europe, with Lisbon as its capital and Porto in the north.
Its food is built on fish, especially salt cod.</p></section>""")


class Entry(Item):
    def __init__(self, path, title, content, mimetype="text/html", front=True):
        super().__init__()
        self.path, self.title, self.content, self.mimetype, self.front = path, title, content, mimetype, front

    def get_path(self):
        return self.path

    def get_title(self):
        return self.title

    def get_mimetype(self):
        return self.mimetype

    def get_contentprovider(self):
        return StringProvider(self.content)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: self.front}


def build_zim(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(str(path)).config_indexing(True, "eng") as c:
        c.set_mainpath("Portugal")
        for p, t, html in [("Lisbon", "Lisbon", LISBON), ("Lisbon/Alfama", "Lisbon/Alfama", ALFAMA),
                           ("Porto", "Porto", PORTO), ("Osaka", "Osaka", OSAKA), ("Portugal", "Portugal", PORTUGAL)]:
            c.add_item(Entry(p, t, html))
        c.add_item(Entry("_res_/style.css", "", "body{color:black}", "text/css", False))
        c.add_item(Entry("_res_/evil.js", "", "alert(1)", "application/javascript", False))
        c.add_redirection("Lisboa", "Lisboa", "Lisbon", {Hint.FRONT_ARTICLE: True})
        for k, v in {"Title": "Test Voyage", "Name": "testvoyage_en_all", "Language": "eng",
                     "Date": "2026-01-01", "Source": "en.wikivoyage.org", "Description": "Tests",
                     "Creator": "tests", "Publisher": "tests"}.items():
            c.add_metadata(k, v)


build_zim(ZIM_DIR / f"{ZIM_ID}.zim")

from fastapi.testclient import TestClient  # noqa: E402

import app as guide  # noqa: E402
import ask as asking  # noqa: E402
import download  # noqa: E402
from library import passages  # noqa: E402

c = TestClient(guide.app)
c.__enter__()  # run the lifespan (opens the ZIM)


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text[:300])
    return r.json()


# ---------------- model stub ----------------
class FakeLLM:
    def __init__(self):
        self.reply = None      # dict -> JSON answer; Exception -> raised
        self.requests = []

    def handler(self, request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.reply)}}]})


llm = FakeLLM()
guide.client = httpx.AsyncClient(transport=httpx.MockTransport(llm.handler))
guide._llm_cache["value"] = None


# ---------------- tests ----------------
def test_health():
    h = ok(c.get("/health"))
    assert h["content_ready"] is True, h
    z = h["zims"][0]
    assert z["id"] == ZIM_ID and z["title"] == "Test Voyage" and z["fulltext"], z
    assert h["download"]["state"] == "off" and h["llm"] == "ready", h


def test_suggest_and_search():
    res = ok(c.get("/api/suggest?q=Lis"))["results"]
    titles = [r["title"] for r in res]
    assert "Lisbon" in titles and "Lisboa" not in titles, "redirects show as their target"
    assert len(titles) == len(set(titles)), titles
    assert ok(c.get("/api/suggest?q=porto"))["results"][0]["title"] == "Porto", "exact match first"
    res = ok(c.get("/api/search?q=francesinha"))["results"]
    assert res and res[0]["path"] == "Porto", res
    assert "<b>" in res[0]["snippet"] and "<script" not in res[0]["snippet"]
    assert ok(c.get("/api/search?q="))["results"] == []


def test_article_is_sanitized():
    r = c.get(f"/z/{ZIM_ID}/Lisbon")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "script-src 'none'" in r.headers["content-security-policy"]
    html = r.text.lower()
    for bad in ("<script", "onclick", "onload", "javascript:", "<iframe", "evil.example", "<style",
                "background:red", "data:image/svg", "<svg", "menu stuff", "footer stuff", "[edit]"):
        assert bad not in html, bad
    t = r.text
    assert 'href="Porto"' in t and 'href="Lisbon/Alfama"' in t, "internal links stay relative"
    assert 'href="./Porto"' in t, "absolute links point at the ZIM's root"
    assert 'href="../../static/article.css"' in t and 'action="../../"' in t
    assert 'target="_blank"' in t and "ext" in t, "external links are marked"
    assert 'href="geo:38.72,-9.13"' in t, "geo: links open a map app"
    assert "pastel de nata" in t and "CC BY-SA" in t and "en.wikivoyage.org/wiki/Lisbon" in t


def test_subpage_and_redirect_and_raw_files():
    t = c.get(f"/z/{ZIM_ID}/Lisbon/Alfama").text
    assert 'href="../Lisbon"' in t and 'href="../../../static/article.css"' in t
    r = c.get(f"/z/{ZIM_ID}/Lisboa", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "Lisbon", r.headers
    css = c.get(f"/z/{ZIM_ID}/_res_/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "sandbox" in css.headers["content-security-policy"]
    assert c.get(f"/z/{ZIM_ID}/_res_/evil.js").status_code == 404, "scripts are never served"
    assert c.get(f"/z/{ZIM_ID}/Nowhere").status_code == 404
    assert c.get("/z/nope/Lisbon").status_code == 404


def test_passages():
    ps = passages(LISBON)
    heads = {h for h, _a, _t in ps}
    assert {"Get in › By plane", "Eat", "Drink", "Sleep"} <= heads, heads
    eat = [t for h, a, t in ps if h == "Eat"]
    assert any("pastel de nata" in t for t in eat) and any("Cervejaria Ramiro" in t for t in eat)
    assert not any("alert" in t or "Menu stuff" in t for _h, _a, t in ps)
    assert all(a for _h, a, _t in ps[1:]), "passages after the intro carry their section anchor"


def test_intents_and_places():
    assert asking.intents("What should I eat in Lisbon?") == ["eat"]
    assert "get in" in asking.intents("How do I get from the airport to the centre in Osaka?")
    assert asking.place_candidates("Is the tap water safe in Mexico City?") == ["Mexico City", "Mexico", "City"]
    assert asking.place_candidates("what to eat in lisbon") == []


def ask(q):
    return ok(c.post("/api/ask", json={"question": q}))


def test_ask_finds_the_right_passages():
    d = ask("What should I eat in Lisbon?")
    top = d["passages"][0]
    assert top["path"] == "Lisbon" and top["section"] == "Eat", top
    assert d["articles"][0]["path"] == "Lisbon" and d["id"] and d["model"] is True
    # lowercase place names work too
    d = ask("what to eat in lisbon")
    assert d["passages"][0]["path"] == "Lisbon" and d["passages"][0]["section"] == "Eat", d["passages"][0]
    d = ask("How do I get from the airport to the centre in Osaka?")
    top = d["passages"][0]
    assert top["path"] == "Osaka" and top["section"].startswith("Get in"), top
    # budget: passages fit the prompt
    assert sum(asking.tokens(p["text"]) for p in d["passages"]) <= guide.PASSAGE_TOKENS
    d = ask("zzqx blorf")
    assert d["passages"] == [] and d["id"] is None


def test_place_names_still_count_outside_their_own_article():
    """'Kansai airport' in the Osaka article must beat Osaka's Itami airport section, even
    though Kansai and Osaka are both places named in the question."""
    def cand(title, section, text):
        return {"zim": "z", "path": title, "title": title, "section": section, "anchor": None,
                "text": text, "place": True}
    cands = [
        cand("Osaka", "Get in › Itami Airport", "Itami Airport is the city's older airport. From the airport, "
             "the monorail and airport buses run towards the city; a taxi to the city center is quicker."),
        cand("Osaka", "Get in › Kansai International Airport", "From Kansai, the JR Haruka express and the "
             "Nankai rapi:t train run to the city in about 40 minutes."),
        cand("Kansai", "Get in › By plane", "There are three airports in the region. Most international "
             "flights land at the main one, on an artificial island in Osaka Bay."),
    ]
    scored = asking.score_passages(cands, "How do I get from Kansai airport to Osaka city centre?",
                                   places=["Kansai", "Osaka"])
    assert scored[0][1]["section"] == "Get in › Kansai International Airport", [(round(s, 2), c["section"]) for s, c in scored]


def test_answer_from_model():
    d = ask("What should I eat in Lisbon?")
    llm.reply = {"answer": "Try pastel de nata, the custard tart, and grilled sardines; bacalhau "
                           "(salt cod) is on every menu. [1]", "sources": [1, 9]}
    a = ok(c.post("/api/answer", json={"id": d["id"]}))
    assert a["status"] == "ok" and "[1]" not in a["answer"] and a["sources"] == [1], a
    body = llm.requests[-1]
    assert body["cache_prompt"] is True and body["response_format"]["type"] == "json_schema"
    assert "pastel de nata" in body["messages"][1]["content"] and "Question: What should I eat" in body["messages"][1]["content"]
    n = len(llm.requests)
    assert ok(c.post("/api/answer", json={"id": d["id"]}))["status"] == "ok" and len(llm.requests) == n, "answers are cached"


def test_answer_guards():
    d = ask("What should I eat in Lisbon?")
    llm.reply = {"answer": "A pastel de nata costs 4.50 euro at the market.", "sources": [1]}
    a = ok(c.post("/api/answer", json={"id": d["id"]}))
    assert a["status"] == "rejected", "made-up numbers are rejected"
    d = ask("What should I eat in Lisbon?")
    llm.reply = {"answer": "Visitors should definitely sample spaghetti carbonara, gelato, pizza "
                           "margherita and tiramisu everywhere.", "sources": [1]}
    assert ok(c.post("/api/answer", json={"id": d["id"]}))["status"] == "rejected", "ungrounded answers are rejected"
    d = ask("What should I eat in Lisbon?")
    llm.reply = httpx.ConnectError("down")
    assert ok(c.post("/api/answer", json={"id": d["id"]}))["status"] == "offline"
    d = ask("What should I eat in Lisbon?")
    llm.reply = httpx.ReadTimeout("slow")
    assert ok(c.post("/api/answer", json={"id": d["id"]}))["status"] == "timeout"
    assert c.post("/api/answer", json={"id": "nope"}).status_code == 404
    s = ok(c.get("/api/stats"))
    assert s["rejected_by"].get("number not in passages") and s["rejected_by"].get("not grounded"), s


def test_answer_busy():
    d = ask("What should I eat in Lisbon?")

    async def hold_and_ask():
        await guide.model_turn.acquire()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guide.app), base_url="http://t") as ac:
                return (await ac.post("/api/answer", json={"id": d["id"]})).json()
        finally:
            guide.model_turn.release()

    assert asyncio.run(hold_and_ask())["status"] == "busy"


def test_no_model():
    saved = guide.LLM_URL
    guide.LLM_URL = ""
    try:
        d = ask("What should I eat in Lisbon?")
        assert d["model"] is False and d["passages"], "without a model, the passages are the answer"
        assert ok(c.post("/api/answer", json={"id": d["id"]}))["status"] == "none"
    finally:
        guide.LLM_URL = saved


def test_index_page_uses_relative_urls():
    t = c.get("/").text
    for absolute in ('fetch("/', "href=\"/", "src=\"/", "action=\"/"):
        assert absolute not in t, absolute
    assert "http://" not in t and "https://" not in t, "no outside resources"


# ---------------- downloader ----------------
class FakeMirror:
    """Serves one file with Range support; can drop the connection part-way or ignore ranges."""

    def __init__(self, data):
        self.data = data
        self.cut_after = None
        self.ignore_range = False
        self.sha = hashlib.sha256(data).hexdigest()
        self.requests = []

    def handler(self, request):
        self.requests.append(dict(request.headers))
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{self.sha}  file.zim\n")
        start = 0
        rng = request.headers.get("range")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(self.data):
                return httpx.Response(416)
        body = self.data[start:]
        cut = self.cut_after

        async def stream():
            sent = 0
            for i in range(0, len(body), 1000):
                chunk = body[i:i + 1000]
                if cut is not None and sent + len(chunk) > cut:
                    yield chunk[: cut - sent]
                    raise httpx.ReadError("connection dropped")
                sent += len(chunk)
                yield chunk

        headers = {"content-length": str(len(body))}
        if start:
            headers["content-range"] = f"bytes {start}-{len(self.data) - 1}/{len(self.data)}"
            return httpx.Response(206, headers=headers, content=stream())
        return httpx.Response(200, headers=headers, content=stream())


def test_download_resumes_verifies_and_replaces_older():
    d = TMP / "dl"
    d.mkdir()
    (d / "voyage_en_all_2025-01.zim").write_bytes(b"old")
    (d / "other_2020-01.zim").write_bytes(b"keep")
    data = os.urandom(25_000)
    mirror = FakeMirror(data)
    mirror.cut_after = 12_000
    done = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(mirror.handler))
    url = "https://mirror.test/zim/voyage_en_all_2026-09.zim"
    dl = download.Downloader([url], d, lambda: done.append(1), client, retry_after=(0.01,))

    async def go():
        task = asyncio.create_task(dl.run())
        for _ in range(200):  # first attempt drops part-way
            await asyncio.sleep(0.01)
            if dl.state == "waiting":
                break
        assert dl.state == "waiting" and "dropped" in dl.error, dl.status()
        assert (d / "voyage_en_all_2026-09.zim.part").stat().st_size == 12_000
        mirror.cut_after = None
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert (d / "voyage_en_all_2026-09.zim").read_bytes() == data
    assert [h.get("range") for h in mirror.requests][:2] == [None, "bytes=12000-"], "resumed with a Range request"
    assert not (d / "voyage_en_all_2025-01.zim").exists(), "the older dated file is replaced"
    assert (d / "other_2020-01.zim").exists(), "other ZIMs are left alone"
    assert dl.status()["state"] == "done" and done


def test_download_restarts_when_range_ignored_and_rejects_bad_checksum():
    d = TMP / "dl2"
    d.mkdir()
    data = os.urandom(5_000)
    mirror = FakeMirror(data)
    mirror.ignore_range = True
    (d / "x_2026-01.zim.part").write_bytes(data[:2000])
    client = httpx.AsyncClient(transport=httpx.MockTransport(mirror.handler))
    dl = download.Downloader(["https://m.test/x_2026-01.zim"], d, lambda: None, client)
    asyncio.run(dl.fetch("https://m.test/x_2026-01.zim"))
    assert (d / "x_2026-01.zim").read_bytes() == data, "a server that ignores Range gets a clean restart"

    dl = download.Downloader(["https://m.test/y_2026-01.zim"], d, lambda: None, client, sha256="0" * 64)
    try:
        asyncio.run(dl.fetch("https://m.test/y_2026-01.zim"))
        raise AssertionError("bad checksum accepted")
    except IOError as e:
        assert "checksum" in str(e)
    assert not (d / "y_2026-01.zim").exists() and not (d / "y_2026-01.zim.part").exists()
    try:
        download.zim_name("https://m.test/not-a-zim.txt")
        raise AssertionError("accepted a non-.zim URL")
    except ValueError:
        pass


def test_not_ready_without_content():
    lib = guide.library
    saved = lib.zims
    lib.zims = {}
    try:
        assert ok(c.get("/health"))["content_ready"] is False
        r = c.post("/api/ask", json={"question": "What should I eat in Lisbon?"})
        assert r.status_code == 503 and "guide" in r.json()["detail"], r.text
        assert c.get("/api/search?q=porto").status_code == 503
        assert c.get("/").status_code == 200, "the page itself always loads"
    finally:
        lib.zims = saved


if __name__ == "__main__":
    t0 = time.time()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print(f"all passed in {time.time() - t0:.1f}s")
