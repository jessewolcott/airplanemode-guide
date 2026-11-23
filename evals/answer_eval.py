"""Score the model's answers the way /api/answer uses them: same prompt, schema, sampling and check.

Rerun this when the shared model, the prompt, the sampling or check_answer() changes, and record
the numbers (llm/ask-pipeline.md). Each question runs --samples times (default 3).

The passages are fixed in answer_cases.json (gathered once from the Wikivoyage ZIM), so every
run and every model sees the same input. Run it in the guide image, which has libzim, on the dev
stack's network so it reaches the shared model:

  docker run --rm --network airplanemode -v "<repo>:/work" -w /work airplanemode-guide:dev \
      python evals/answer_eval.py http://llm:8080
  ... python evals/answer_eval.py URL --show --samples 1        # print every answer
  ... -v airplanemode_guide-data:/data:ro ... python evals/answer_eval.py URL --gather /data/zim
                                            # refresh the passages after a retrieval change

Each sample ends up as one of:
  right     shown, has a right marker (or an honest "not covered" when the guide has no answer),
            and no wrong marker
  honest    the guide doesn't cover it, and the app said so (status not_covered)
  missed    covered, but the app showed no answer (not_covered or rejected)
  wrong     shown, but has a wrong marker, or claims an answer the passages don't have
  dropped   rejected by the check or bad output where nothing was right to show anyway
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ask  # noqa: E402

CASES = ROOT / "evals" / "answer_cases.json"


def outcome(case, status, text):
    low = (text or "").lower()
    if status == "ok":
        if any(w in low for w in case["wrong"]):
            return "wrong"
        if case["covered"]:
            return "right" if any(w in low for w in case["right_any"]) else "wrong"
        # not covered: only an honest answer (or the one real link, right_any) is right
        if ask.says_not_covered(text) or any(w in low for w in case["right_any"]):
            return "right"
        return "wrong"
    if status == "not_covered":
        return "missed" if case["covered"] else "honest"
    return "missed" if case["covered"] else "dropped"


def answer(url, case):
    """One /api/answer, minus the web server. Returns (status, text, raw, seconds)."""
    picked = case["passages"]
    body = {"messages": ask.prompt(case["question"], picked), "max_tokens": 220, "temperature": 0.2,
            "repeat_penalty": 1.1, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": ask.schema(len(picked))}}}
    t = time.time()
    raw = ""
    try:
        r = httpx.post(f"{url}/v1/chat/completions", json=body, timeout=300)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("bad output")
        text, _ = ask.check_answer(data.get("answer"), data.get("sources"), picked)
        return "ok", text, raw, time.time() - t
    except ask.NotCovered:
        return "not_covered", "", raw, time.time() - t
    except ValueError as e:
        return f"rejected: {e}"[:60], "", raw, time.time() - t


def gather(zim_dir, path):
    from library import Library
    lib = Library(Path(zim_dir), 8)
    lib.reload()
    cases = json.loads(path.read_text(encoding="utf-8"))
    for c in cases:
        f = ask.gather(lib, c["question"], 900)
        c["articles"] = [a["title"] for a in f["articles"]]
        c["passages"] = f["passages"]
        print(c["question"], "->", c["articles"], len(c["passages"]))
    path.write_text(json.dumps(cases, indent=1, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="llama.cpp base URL, e.g. http://llm:8080")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--show", action="store_true", help="print every answer")
    ap.add_argument("--gather", metavar="ZIM_DIR", help="refresh the passages from the ZIM files first")
    ap.add_argument("--cases", default=str(CASES),
                    help="cases file (default answer_cases.json, tuned against; answer_check.json is the check set)")
    ap.add_argument("--out", help="write every result to this JSON file")
    args = ap.parse_args()
    if args.gather:
        gather(args.gather, Path(args.cases))
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    total, rows, secs = Counter(), [], []
    for c in cases:
        per = Counter()
        for _ in range(args.samples):
            status, text, raw, s = answer(args.url, c)
            secs.append(s)
            o = outcome(c, status, text)
            per[o] += 1
            total[o] += 1
            rows.append({"question": c["question"], "status": status, "outcome": o, "answer": text, "raw": raw})
            if args.show:
                print(f"  [{o}] {status}: {text or raw}")
        print(f"{c['question'][:58]:58} {dict(per)}", flush=True)
    n = sum(total.values())
    print(f"\n{n} samples: " + ", ".join(f"{k} {total[k]}" for k in ("right", "honest", "missed", "wrong", "dropped")),
          f"| mean {sum(secs) / len(secs):.1f} s")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
