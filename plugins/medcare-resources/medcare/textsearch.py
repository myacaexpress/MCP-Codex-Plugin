"""Small dependency-free BM25 index with snippet extraction."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict

_TOKEN = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")
STOP = frozenset("""a an and are as at be by for from has have if in into is it its of on or
that the their this to was were will with you your our we y el la los las de del en para por
un una que se""".split())


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().replace("‑", "-").replace("–", "-").replace("—", "-")


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    for tok in _TOKEN.findall(fold(text)):
        tok = tok.replace("'", "")
        if keep_stop or tok not in STOP:
            out.append(tok)
    return out


class Index:
    def __init__(self, docs: list[dict], fields=("text",), weights=None):
        self.docs = docs
        self.weights = weights or {f: 1.0 for f in fields}
        self.postings: dict[str, dict[int, float]] = defaultdict(dict)
        self.lengths: list[float] = []
        for i, doc in enumerate(docs):
            counts: Counter = Counter()
            for f, w in self.weights.items():
                for tok in tokens(str(doc.get(f) or "")):
                    counts[tok] += w
            self.lengths.append(sum(counts.values()) or 1.0)
            for tok, c in counts.items():
                self.postings[tok][i] = c
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0

    def search(self, query: str, limit: int = 10, filt=None, phrase_boost: bool = True):
        q = tokens(query)
        if not q:
            q = tokens(query, keep_stop=True)
        if not q:
            return []
        n = len(self.docs) or 1
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, set] = defaultdict(set)
        for tok in set(q):
            post = self.postings.get(tok)
            if not post:
                # prefix fallback for partial words (e.g. 'metform')
                if len(tok) >= 4:
                    post = {}
                    for key, p in self.postings.items():
                        if key.startswith(tok):
                            for i, c in p.items():
                                post[i] = post.get(i, 0) + c
                if not post:
                    continue
            idf = math.log(1 + (n - len(post) + 0.5) / (len(post) + 0.5))
            for i, tf in post.items():
                if filt and not filt(self.docs[i]):
                    continue
                denom = tf + 1.2 * (0.25 + 0.75 * self.lengths[i] / self.avg)
                scores[i] += idf * tf * 2.2 / denom
                matched[i].add(tok)
        qset = set(q)
        phrase = fold(query).strip()
        ranked = []
        for i, s in scores.items():
            coverage = len(matched[i]) / len(qset)
            s *= 0.5 + coverage
            if phrase_boost and len(phrase) > 3:
                body = fold(" ".join(str(self.docs[i].get(f) or "") for f in self.weights))
                if phrase in body:
                    s *= 1.6
            ranked.append((s, coverage, i))
        ranked.sort(reverse=True)
        return [(self.docs[i], round(s, 4), round(c, 3)) for s, c, i in ranked[:limit]]


def snippet(text: str, query: str, width: int = 320) -> str:
    """Return the passage around the densest cluster of query terms."""
    text = re.sub(r"[ \t]+", " ", text or "")
    if len(text) <= width:
        return text.strip()
    folded = fold(text)
    q = [t for t in tokens(query)] or tokens(query, keep_stop=True)
    phrase = fold(query).strip()
    pos = folded.find(phrase) if len(phrase) > 3 else -1
    if pos < 0:
        hits = [m.start() for t in q for m in re.finditer(re.escape(t), folded)]
        if not hits:
            return text[:width].strip() + " …"
        hits.sort()
        best, best_n = hits[0], 0
        for h in hits:
            n = sum(1 for x in hits if h <= x < h + width)
            if n > best_n:
                best, best_n = h, n
        pos = best
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    return ("… " if start else "") + text[start:end].strip() + (" …" if end < len(text) else "")


def matching_lines(text: str, query: str, max_lines: int = 8, context: int = 1) -> list[str]:
    """Lines containing any query token (with neighbouring lines for table context)."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    q = set(tokens(query)) or set(tokens(query, keep_stop=True))
    phrase = fold(query).strip()
    picked: list[int] = []
    scored = []
    for i, ln in enumerate(lines):
        f = fold(ln)
        toks = set(tokens(ln))
        hit = len(q & toks) + (2 if len(phrase) > 3 and phrase in f else 0)
        if hit:
            scored.append((hit, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _, i in scored[:max_lines]:
        picked.extend(range(max(0, i - context), min(len(lines), i + context + 1)))
    out, seen = [], set()
    for i in sorted(set(picked)):
        if lines[i] and i not in seen:
            out.append(lines[i])
            seen.add(i)
    return out[: max_lines * (2 * context + 1)]


def line_coverage(text: str, query: str) -> float:
    """Best fraction of query tokens found within a 3-line window (tables wrap cells across lines)."""
    q = set(tokens(query)) or set(tokens(query, keep_stop=True))
    if not q:
        return 0.0
    lines = [set(tokens(ln)) for ln in (text or "").splitlines()]
    best = 0.0
    for i in range(len(lines)):
        window = set().union(*lines[i:i + 3]) if lines[i:i + 3] else set()
        hit = sum(1 for t in q if t in window or any(w.startswith(t) for w in window if len(t) >= 4))
        best = max(best, hit / len(q))
    return best


def rerank(hits, query: str, field: str = "text"):
    """Re-rank BM25 hits so pages where the query terms co-occur on nearby lines come first."""
    scored = [(line_coverage(doc.get(field, ""), query), score, doc, cov) for doc, score, cov in hits]
    scored.sort(key=lambda x: (round(x[0], 2), x[1]), reverse=True)
    return [(doc, score, cov) for lc, score, doc, cov in scored]
