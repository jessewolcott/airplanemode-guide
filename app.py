"""Travel Guide: an AirplaneMode app. Offline Wikivoyage (Kiwix ZIM files) you can browse and
search, plus "ask the guide", where the shared on-board model answers from the guide's own
passages.

Pages (all URLs relative: the app runs behind a path prefix, /guide/ on the box):
  /                     search, ask, and download progress
  /z/<zim>/<path>       an article (sanitized, no scripts) or one of its CSS/image files
API:
  GET  /health          {"content_ready", "zims", "download", "llm"}   (update.sh waits on this)
  GET  /api/suggest?q=  title suggestions
  GET  /api/search?q=   full-text results with snippets
  POST /api/ask         {question} -> passages, at once, plus an id for the model's answer
  POST /api/answer      {id}       -> the model's answer from those passages (or why not)
  GET  /api/stats       how often the model's answers are used or rejected
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ask as asking
from download import DEFAULT_ZIM_URL, Downloader
from library import (ARTICLE_CSP, RAW_CSP, Library, NotReady, heavy, prefix_for, raw_allowed,
                     render_article, trim_memory)

log = logging.getLogger("guide")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = Path(__file__).parent
ZIM_DIR = Path(os.getenv("ZIM_DIR", "/data/zim"))
ZIM_URLS = os.getenv("ZIM_URL", DEFAULT_ZIM_URL).split()          # space-separated; empty = never download
ZIM_SHA256 = os.getenv("ZIM_SHA256", "")
LLM_URL = os.getenv("LLM_URL", "http://llm:8080").rstrip("/")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))             # the R6S is slow: ~1 min for an answer
LLM_WAIT = float(os.getenv("LLM_WAIT", "3"))                    # wait this long for our turn, else "busy"
PASSAGE_TOKENS = int(os.getenv("PASSAGE_TOKENS", "900"))        # prompt budget for the passages
ANSWER_TOKENS = int(os.getenv("ANSWER_TOKENS", "220"))
CLUSTER_CACHE_MB = int(os.getenv("CLUSTER_CACHE_MB", "8"))

client = httpx.AsyncClient(timeout=LLM_TIMEOUT)
library = Library(ZIM_DIR, CLUSTER_CACHE_MB)
downloader = Downloader(ZIM_URLS, ZIM_DIR, library.reload, client, ZIM_SHA256)
model_turn = asyncio.Semaphore(1)  # one model call at a time from this app: the model is shared
asks: "OrderedDict[str, dict]" = OrderedDict()
STATS = {"asks": 0, "answers": Counter(), "rejected_by": Counter()}


@asynccontextmanager
async def lifespan(_app):
    ZIM_DIR.mkdir(parents=True, exist_ok=True)
    await run_in_threadpool(library.reload)
    task = asyncio.create_task(downloader.run()) if downloader.state != "off" else None
    yield
    if task:
        task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def not_ready():
    d = downloader.status()
    if d["state"] in ("downloading", "verifying") and d["percent"] is not None:
        msg = f"The guide is still downloading ({d['percent']:.0f}%)."
    elif d["state"] == "waiting":
        msg = "The guide hasn't been downloaded yet. It will be, the next time the box is online."
    else:
        msg = "No guide content on this box yet."
    return HTTPException(503, msg)


# ---------------- status ----------------
_llm_cache = {"at": 0.0, "value": None}


async def llm_status():
    if not LLM_URL:
        return {"llm": "none", "model": None}
    now = time.monotonic()
    if _llm_cache["value"] and now - _llm_cache["at"] < 30:
        return _llm_cache["value"]
    try:
        r = await client.get(f"{LLM_URL}/v1/models", timeout=3)
        r.raise_for_status()
        value = {"llm": "ready", "model": r.json()["data"][0]["id"]}
    except Exception:
        value = {"llm": "offline", "model": None}
    _llm_cache.update(at=now, value=value)
    return value


@app.get("/health")
async def health():
    return {"content_ready": library.ready, "zims": [z.info() for z in library.zims.values()],
            "download": downloader.status(), **(await llm_status())}


@app.get("/api/stats")
async def stats():
    a = STATS["answers"]
    used = a["ok"]
    tried = sum(a.values())
    return {"asks": STATS["asks"], "answers": dict(a), "rejected_by": dict(STATS["rejected_by"]),
            "accept_rate": round(used / tried, 3) if tried else None}


# ---------------- search ----------------
@app.get("/api/suggest")
async def suggest(q: str = ""):
    q = q.strip()[:100]
    if not q:
        return {"results": []}
    try:
        return {"results": await run_in_threadpool(library.suggest, q)}
    except NotReady:
        raise not_ready()


@app.get("/api/search")
async def search(q: str = ""):
    q = q.strip()[:200]
    if not q:
        return {"results": []}
    try:
        return {"results": await run_in_threadpool(library.search, q, 25)}
    except NotReady:
        raise not_ready()


# ---------------- ask ----------------
class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=300)


class AnswerReq(BaseModel):
    id: str


@app.post("/api/ask")
async def ask(body: Ask):
    if not library.ready:
        raise not_ready()
    STATS["asks"] += 1
    found = await run_in_threadpool(_heavy, asking.gather, library, body.question, PASSAGE_TOKENS)
    aid = uuid.uuid4().hex
    if found["passages"]:
        asks[aid] = {"question": body.question, "passages": found["passages"]}
        while len(asks) > 100:
            asks.popitem(last=False)
    return {"id": aid if found["passages"] else None, "articles": found["articles"],
            "passages": found["passages"], "model": bool(LLM_URL) and bool(found["passages"])}


async def chat(messages, max_tokens, schema):
    if not LLM_URL:
        raise RuntimeError("no LLM configured")
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.2,
            "repeat_penalty": 1.1, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}
    r = await client.post(f"{LLM_URL}/v1/chat/completions", json=body, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


@app.post("/api/answer")
async def answer(body: AnswerReq):
    a = asks.get(body.id)
    if not a:
        raise HTTPException(404, "That question has expired; ask again.")
    if "answer" in a:
        return a["answer"]
    if not LLM_URL:
        return {"status": "none", "detail": "There's no on-board model on this box."}
    try:
        await asyncio.wait_for(model_turn.acquire(), LLM_WAIT)
    except asyncio.TimeoutError:
        STATS["answers"]["busy"] += 1
        return {"status": "busy", "detail": "The on-board model is busy with another question."}
    t0 = time.monotonic()
    raw = ""
    try:
        picked = a["passages"]
        raw = await chat(asking.prompt(a["question"], picked), ANSWER_TOKENS, asking.schema(len(picked)))
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("bad output")
        text, sources = asking.check_answer(data.get("answer"), data.get("sources"), picked)
    except asking.NotCovered:  # an honest "the guide doesn't cover this": say so, keep the passages
        STATS["answers"]["not_covered"] += 1
        out = {"status": "not_covered", "detail": "The on-board model found no answer to that in the guide."}
        a["answer"] = out
        return out
    except ValueError as e:  # bad JSON or failed the grounding check
        reason = str(e) if str(e) in ("empty", "number not in passages", "not grounded") else "bad output"
        STATS["answers"]["rejected"] += 1
        STATS["rejected_by"][reason] += 1
        log.info("answer rejected (%s): %.300r", reason, raw)
        return {"status": "rejected", "detail": "The model's answer didn't stick to the guide, so here's the guide itself."}
    except (httpx.TimeoutException, asyncio.TimeoutError):
        STATS["answers"]["timeout"] += 1
        return {"status": "timeout", "detail": "The on-board model took too long."}
    except Exception as e:
        STATS["answers"]["offline"] += 1
        log.info("model unavailable: %s", e)
        return {"status": "offline", "detail": "The on-board model isn't available right now."}
    finally:
        model_turn.release()
    STATS["answers"]["ok"] += 1
    out = {"status": "ok", "answer": text, "sources": sources, "seconds": round(time.monotonic() - t0, 1)}
    a["answer"] = out
    return out


# ---------------- articles and their files ----------------
def _heavy(fn, *args):
    with heavy:
        try:
            return fn(*args)
        finally:
            trim_memory()


def _render(z, zim_id, path):
    _z, title, real, raw = library.article_html(zim_id, path)
    return render_article(z, title, real, raw, prefix_for(path))

@app.get("/z/{zim_id}/{path:path}")
async def zim_entry(zim_id: str, path: str):
    z, e = library.entry(zim_id, path)
    if not z:
        raise not_ready() if not library.ready else HTTPException(404, "No such guide.")
    if not e:
        raise HTTPException(404, "That page isn't in the guide.")
    if e.is_redirect:
        target = e.get_redirect_entry().path
        return RedirectResponse("../" * path.count("/") + quote(target), status_code=302)
    item = e.get_item()
    mt = item.mimetype
    if mt.startswith("text/html"):
        page = await run_in_threadpool(_heavy, _render, z, zim_id, path)
        return HTMLResponse(page, headers={"Content-Security-Policy": ARTICLE_CSP,
                                           "X-Content-Type-Options": "nosniff",
                                           "Referrer-Policy": "no-referrer",
                                           "Cache-Control": "private, max-age=3600"})
    if not raw_allowed(mt):
        raise HTTPException(404, "Not served.")
    return Response(bytes(item.content), media_type=mt,
                    headers={"Content-Security-Policy": RAW_CSP, "X-Content-Type-Options": "nosniff",
                             "Cache-Control": "public, max-age=86400"})


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})
