"""Background download of the guide's ZIM file(s), so the box has them before it goes offline.

- Runs in the background; the app starts and serves (with a progress banner) meanwhile.
- Resumable: data goes to <name>.zim.part and continues with an HTTP Range request after a
  restart or a dropped connection. Only renamed to <name>.zim once it's complete and checked.
- Checked: size against the server's, and SHA-256 when known (ZIM_SHA256, the pinned hash
  for the default URL, or Kiwix's <url>.sha256 file).
- Updating: when a newer dated file of the same ZIM arrives (wikivoyage_en_all_nopic_2026-09
  replacing _2026-06), the older one is removed once the new one opens.
- Offline: failures are retried with backoff, forever, quietly.
"""
import asyncio
import hashlib
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("guide")

DEFAULT_ZIM_URL = "https://download.kiwix.org/zim/wikivoyage/wikivoyage_en_all_nopic_2026-09.zim"
PINNED_SHA256 = {DEFAULT_ZIM_URL: "4305c50550f065d83e70f75fd1d2016a694f5d67aa1e0ef00fdf6bb74fc092ac"}
DATED = re.compile(r"^(?P<base>.+)_(?P<date>\d{4}-\d{2})$")
CHUNK = 1 << 20


def zim_name(url):
    name = Path(urlsplit(url).path).name
    if not name.endswith(".zim") or "/" in name or name.startswith("."):
        raise ValueError(f"ZIM_URL must point at a .zim file: {url}")
    return name


class Downloader:
    def __init__(self, urls, zim_dir: Path, on_done, client: httpx.AsyncClient, sha256="",
                 retry_after=(30, 60, 300, 900)):
        self.urls = [u for u in urls if u]
        self.zim_dir = zim_dir
        self.on_done = on_done            # called (sync) after a file is in place
        self.client = client
        self.sha256 = sha256.strip().lower()
        self.retry_after = retry_after
        self.state = "idle" if self.urls else "off"
        self.file = None
        self.done = 0
        self.total = None
        self.rate = None
        self.error = None
        self.next_try = None

    def status(self):
        pct = round(100 * self.done / self.total, 1) if self.total else None
        eta = round((self.total - self.done) / self.rate) if self.total and self.rate else None
        return {"state": self.state, "file": self.file, "done": self.done, "total": self.total,
                "percent": pct, "rate": round(self.rate) if self.rate else None, "eta_s": eta,
                "error": self.error,
                "retry_in_s": max(0, round(self.next_try - time.monotonic())) if self.next_try else None}

    def missing(self):
        return [u for u in self.urls if not (self.zim_dir / zim_name(u)).exists()]

    async def run(self):
        """Fetch every missing file; retry failures with backoff. Never raises."""
        attempt = 0
        while True:
            todo = self.missing()
            if not todo:
                if self.state != "off":
                    self.state = "done"
                self.error = self.next_try = None
                return
            try:
                await self.fetch(todo[0])
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state, self.error = "waiting", short_error(e)
                wait = self.retry_after[min(attempt, len(self.retry_after) - 1)]
                attempt += 1
                log.warning("download of %s failed (%s); retrying in %ds", self.file, self.error, wait)
                self.next_try = time.monotonic() + wait
                await asyncio.sleep(wait)
                self.next_try = None

    async def expected_sha(self, url):
        if self.sha256:
            return self.sha256
        if url in PINNED_SHA256:
            return PINNED_SHA256[url]
        try:  # Kiwix publishes <file>.sha256 next to each ZIM
            r = await self.client.get(url + ".sha256", follow_redirects=True, timeout=20)
            if r.status_code == 200:
                m = re.match(r"\s*([0-9a-fA-F]{64})\b", r.text)
                if m:
                    return m.group(1).lower()
        except Exception:
            pass
        return None

    async def fetch(self, url):
        name = zim_name(url)
        self.file, self.error = name, None
        self.zim_dir.mkdir(parents=True, exist_ok=True)
        part = self.zim_dir / (name + ".part")
        have = part.stat().st_size if part.exists() else 0
        self.state, self.done, self.total, self.rate = "downloading", have, None, None
        headers = {"Range": f"bytes={have}-"} if have else {}
        timeout = httpx.Timeout(30, read=60)
        async with self.client.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout) as r:
            if r.status_code == 416 and have:  # we already have all of it
                total = have
            elif r.status_code == 206 and have:
                m = re.match(r"bytes (\d+)-\d+/(\d+)", r.headers.get("content-range", ""))
                if not m or int(m.group(1)) != have:
                    raise IOError("server sent the wrong range")
                total = int(m.group(2))
            elif r.status_code == 200:
                if have:
                    log.info("server ignored the range request; starting %s again", name)
                have = 0
                total = int(r.headers["content-length"]) if "content-length" in r.headers else None
            else:
                raise IOError(f"HTTP {r.status_code}")
            self.total, self.done = total, have
            if total:
                free = shutil.disk_usage(self.zim_dir).free
                if total - have > free - 50 * 1024 * 1024:
                    raise IOError(f"not enough disk space: need {(total - have) >> 20} MB, "
                                  f"have {free >> 20} MB")
            if r.status_code != 416:
                with open(part, "ab" if have else "wb") as f:
                    t0, n0 = time.monotonic(), have
                    async for chunk in r.aiter_bytes():  # as received: a drop loses nothing
                        f.write(chunk)
                        self.done += len(chunk)
                        dt = time.monotonic() - t0
                        if dt >= 1:
                            inst = (self.done - n0) / dt
                            self.rate = inst if self.rate is None else 0.7 * self.rate + 0.3 * inst
                            t0, n0 = time.monotonic(), self.done
        size = part.stat().st_size
        if self.total is not None and size != self.total:
            raise IOError(f"incomplete: {size} of {self.total} bytes")
        want = await self.expected_sha(url)
        if want:
            self.state = "verifying"
            got = await asyncio.to_thread(sha256_file, part)
            if got != want:
                part.unlink(missing_ok=True)
                self.done = 0
                raise IOError("checksum mismatch; the file was deleted and will be fetched again")
        final = self.zim_dir / name
        part.replace(final)
        log.info("downloaded %s (%d MB)", name, size >> 20)
        self.on_done()
        remove_older(final)
        self.on_done()


def remove_older(new: Path):
    """Delete older dated versions of the same ZIM (same name, earlier _YYYY-MM)."""
    m = DATED.match(new.stem)
    if not m:
        return
    for p in new.parent.glob(m.group("base") + "_*.zim"):
        o = DATED.match(p.stem)
        if p != new and o and o.group("base") == m.group("base") and o.group("date") < m.group("date"):
            log.info("removing the older %s", p.name)
            p.unlink(missing_ok=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def short_error(e):
    msg = str(e) or type(e).__name__
    return msg if len(msg) < 160 else msg[:157] + "…"
