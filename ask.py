"""Ask the guide: find the passages that answer a question, then (optionally) let the model
write a short answer from them.

Everything up to the model is deterministic and fast, and is useful on its own: the page
shows the passages straight away ("Here's what the guide says"), and the model's answer is
added on top when (and if) it arrives.

  question -> places named in it + full-text search -> up to MAX_ARTICLES articles
           -> passages (sections, listings, paragraphs) -> BM25 + section-intent boost
           -> best passages within a token budget -> model answer, checked against them
"""
import math
import re
from collections import Counter

from library import passages as split_passages

STOP = set("""a about above after again against all am an and any are aren't as at be because been
before being below between both but by can can't cannot could couldn't did didn't do does doesn't
doing don't down during each few for from further get gets getting go going got had hadn't has
hasn't have haven't having he her here hers herself him himself his how i i'd i'll i'm i've if in
into is isn't it it's its itself just let's like me more most mustn't my myself near need no nor
not of off on once only or other ought our ours ourselves out over own please same shan't she
should shouldn't so some such tell than that that's the their theirs them themselves then there
there's these they this those through to too under until up very want was wasn't we were weren't
what what's when where where's which while who whom why will with won't would wouldn't you your
yours yourself best good recommend recommended way ways thing things there anything something
around also really much many visit visiting""".split())

# Wikivoyage's standard section names, and the words that point at them.
INTENTS = {
    "eat": ("eat food restaurant restaurants dish dishes cuisine meal meals lunch dinner breakfast "
            "eating hungry vegetarian vegan snack snacks street-food specialty specialties local-food"),
    "drink": "drink drinks bar bars nightlife beer wine cocktail cocktails pub pubs club clubs coffee cafe cafes",
    "sleep": "sleep hotel hotels hostel hostels stay staying accommodation lodging guesthouse camping room rooms",
    "get in": ("airport airports arrive arriving arrival fly flight flights plane train trains station "
               "ferry visa visas border reach entry"),
    "get around": ("around metro subway bus buses tram trams taxi taxis transport transit ticket tickets "
                   "card bike bicycle walk walking drive driving car rental uber centre center downtown"),
    "see": "see sights sight museum museums landmark landmarks attractions monument monuments view views church castle",
    "do": "do activities activity hike hiking beach beaches swim festival festivals tour tours surf ski",
    "buy": "buy shop shopping shops market markets souvenir souvenirs money currency atm cash cost costs price prices tip tipping cheap",
    "stay safe": "safe safety danger dangerous crime scam scams pickpocket pickpockets theft emergency police",
    "stay healthy": "healthy health water hospital doctor pharmacy vaccine vaccines sick tap",
    "connect": "connect wifi wi-fi internet sim phone mobile data esim",
    "talk": "talk language languages speak english phrase phrases",
    "respect": "respect etiquette customs rude polite dress",
    "understand": "understand history climate weather when season seasons culture",
}
INTENT_WORDS = {w: section for section, words in INTENTS.items() for w in words.split()}
# a section heading counts as matching an intent if it starts with these
SECTION_PREFIX = {"get in": ("get in", "by plane", "by train", "by bus", "by boat", "by car", "arriv"),
                  "get around": ("get around", "by public transport", "by metro", "by taxi", "by bus", "by foot")}

MAX_ARTICLES = 3
WORD_RE = re.compile(r"[\w][\w'’-]*", re.UNICODE)


def stem(w):
    w = w.lower().strip("'’-")
    for suf in ("ies", "es", "s"):
        if len(w) > 4 and w.endswith(suf) and not w.endswith("ss"):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def words(text):
    return WORD_RE.findall(text)


def keywords(question):
    """Content words of the question, stemmed, stopwords out."""
    return [stem(w) for w in words(question) if w.lower() not in STOP and len(w) > 1]


def intents(question):
    found = []
    for w in words(question.lower()):
        s = INTENT_WORDS.get(w) or INTENT_WORDS.get(stem(w))
        if s and s not in found:
            found.append(s)
    # "how do I get to X / from the airport" is a "get in" question
    if re.search(r"\bget (to|from|into)\b|\bfrom the airport\b", question.lower()) and "get in" not in found:
        found.append("get in")
    return found


def place_candidates(question):
    """Word sequences that might be place names: capitalised runs (Rio de Janeiro), longest first."""
    toks = words(question)
    out = []
    for n in (4, 3, 2, 1):
        for i in range(len(toks) - n + 1):
            seq = toks[i:i + n]
            if not seq[0][:1].isupper() or not seq[-1][:1].isupper():
                continue
            if n == 1 and (seq[0].lower() in STOP or i == 0 and seq[0].lower() in STOP | {"how", "what"}):
                continue
            out.append(" ".join(seq))
    return list(dict.fromkeys(out))


def _title_in_question(title, question):
    t = re.sub(r"\s*\(.*?\)\s*", " ", title).strip().lower()
    return bool(t) and re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", question.lower()) is not None


def find_articles(lib, question, limit=MAX_ARTICLES):
    """[(zim, path, title, is_place)] for the question: named places first, then full-text hits.
    Places named in lowercase ("what to eat in lisbon") are caught by matching full-text
    result titles against the question."""
    picked, seen = [], set()

    def add(zim_id, path, title, place):
        key = (zim_id, path)
        if key not in seen and len(picked) < limit:
            seen.add(key)
            picked.append((zim_id, path, title, place))

    for cand in place_candidates(question):
        hit = lib.title_entry(cand)
        if hit:
            z, e = hit
            add(z.id, e.path, e.title, True)
        if len(picked) >= 2:
            break
    query = " ".join(w for w in words(question) if w.lower() not in STOP) or question
    try:
        hits = lib.search(query, limit=10)
    except Exception:
        hits = []
    for h in hits:  # lowercase place names
        if _title_in_question(h["title"], question) and "/" not in h["path"]:
            add(h["zim"], h["path"], h["title"], True)
    for h in hits:
        add(h["zim"], h["path"], h["title"], False)
    return picked


def section_matches(head, wanted):
    h = (head or "").lower()
    for s in wanted:
        if h.startswith(s) or any(h.startswith(p) or f"› {p}" in h for p in SECTION_PREFIX.get(s, ())):
            return True
        if f"› {s}" in h:
            return True
    return False


def score_passages(cands, question, k1=1.4, b=0.75, places=()):
    """BM25 over the candidate passages, plus boosts for the section the question is about
    and for the article of a place the question names. Once a place's article is found, the
    place's name only picks that article: it's everywhere in it, so it's left out of scoring
    passages from that article. In other articles it still counts: "Kansai airport" must
    pick the Kansai section of the Osaka article, not the one about Itami airport."""
    q = keywords(question)
    if not cands:
        return []

    def title_words(title):
        return {stem(w) for w in words(re.sub(r"[(].*?[)]", "", title))}
    own = {t: title_words(t) for t in places}
    wanted = intents(question)
    docs = [Counter(stem(w) for w in words(c["text"])) for c in cands]
    lens = [sum(d.values()) for d in docs]
    avg = sum(lens) / len(lens) or 1
    n = len(docs)
    df = Counter(t for d in docs for t in set(d) if t in q)
    scored = []
    for c, d, ln in zip(cands, docs, lens):
        s = 0.0
        terms = set(q)
        if c["place"] and c["title"] in own and terms - own[c["title"]]:
            terms -= own[c["title"]]
        for t in terms:
            if t not in d:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * d[t] * (k1 + 1) / (d[t] + k1 * (1 - b + b * ln / avg))
        if wanted:
            if section_matches(c["section"], wanted):
                s += 2.5
            else:
                s *= 0.6
        if c["place"]:
            s *= 1.6
        if ln < 15:
            s *= 0.7
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return scored


def tokens(text):
    return len(text) // 4 + 1  # rough, but close enough for Qwen-style tokenizers on English


def gather(lib, question, budget_tokens=900, max_passages=6):
    """The deterministic half of "ask": the best passages, already in budget."""
    arts = find_articles(lib, question)
    cands, seen = [], set()
    for zim_id, path, title, place in arts:
        got = lib.article_html(zim_id, path)
        if not got:
            continue
        _z, title, real_path, raw = got
        for head, anchor, text in split_passages(raw):
            if text[:160] in seen:  # region articles repeat each other's itineraries
                continue
            seen.add(text[:160])
            cands.append({"zim": zim_id, "path": real_path, "title": title, "section": head,
                          "anchor": anchor, "text": text, "place": place})
    scored = score_passages(cands, question, places=[t for _z, _p, t, pl in arts if pl])
    out, used = [], 0
    for s, c in scored:
        if s <= 0 or len(out) >= max_passages:
            break
        cost = tokens(c["text"]) + 12
        if used + cost > budget_tokens:
            if out:
                continue  # try smaller ones further down
            c = dict(c, text=trim(c["text"], budget_tokens * 4))
            cost = tokens(c["text"]) + 12
        out.append(dict(c, score=round(s, 2)))
        used += cost
    return {"articles": [{"zim": z, "path": p, "title": t} for z, p, t, _ in arts], "passages": out}


def trim(text, chars):
    if len(text) <= chars:
        return text
    cut = text[:chars]
    return cut[: cut.rfind(" ")] + "…"


# ---------------- the model's half ----------------
SYSTEM = (
    "You are a travel guide assistant on an offline travel router. Answer the traveller's question "
    "using ONLY the numbered passages from the guide. Be practical and brief: 2 to 5 sentences. "
    "Keep names, prices and times exactly as the passages give them. If the passages don't answer "
    "the question, say that the guide doesn't cover it. Reply in JSON: \"answer\" is your answer "
    "(no passage numbers in it), \"sources\" lists the numbers of the passages you used."
)


def schema(n):
    return {"type": "object",
            "properties": {"answer": {"type": "string", "minLength": 60, "maxLength": 900},
                           "sources": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": n},
                                       "maxItems": 4}},
            "required": ["answer", "sources"]}


def prompt(question, picked):
    lines = []
    for i, p in enumerate(picked, 1):
        where = p["title"] + (f" — {p['section']}" if p["section"] else "")
        lines.append(f"[{i}] ({where}) {p['text']}")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Passages:\n" + "\n\n".join(lines) + f"\n\nQuestion: {question.strip()}\n"
                                    "Answer in 2 to 5 full sentences, from the passages only."}]


NUM_RE = re.compile(r"\d+(?:[.,:]\d+)*")
def check_answer(answer, sources, picked):
    """Deterministic guard: the model's answer must stay inside the passages.
    Returns (cleaned answer, sources) or raises ValueError(reason)."""
    answer = re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]", "", (answer or "")).strip()
    if len(answer) < 15:
        raise ValueError("empty")
    text = " ".join(p["text"] + " " + p["title"] + " " + (p["section"] or "") for p in picked)
    nums_src = set(NUM_RE.findall(text.replace(",", "")))
    for num in NUM_RE.findall(answer.replace(",", "")):
        if num not in nums_src and not (len(num) == 1):  # small counts ("2 lines") are fine
            raise ValueError("number not in passages")
    src_words = {stem(w) for w in words(text)}
    content = [stem(w) for w in words(answer) if w.lower() not in STOP and len(w) > 3]
    if content:
        grounded = sum(1 for w in content if w in src_words) / len(content)
        if grounded < 0.6:
            raise ValueError("not grounded")
    srcs = sorted({s for s in (sources or []) if isinstance(s, int) and 1 <= s <= len(picked)})
    return answer, srcs or [1]
