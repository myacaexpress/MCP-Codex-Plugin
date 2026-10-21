"""Stdlib HTML extraction: links with heading context, and visible text lines."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_CHROME = {"header", "footer", "nav"}
_SKIP = {"script", "style", "noscript", "svg", "template"}
_BLOCK = {"p", "div", "li", "tr", "td", "th", "section", "article", "br", "h1", "h2", "h3", "h4",
          "h5", "h6", "ul", "ol", "table", "form", "label", "option", "button", "a", "span"}
_PLAN_HEADING = re.compile(r"^MedCare\s+(Classic|Focus|Choice|Value|Balance|Core)\b", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
         "track", "wbr"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.skip_depth = 0
        self.links: list[dict] = []
        self.current_link: dict | None = None
        self.heading_buf: list[str] | None = None
        self.headings: list[tuple[int, str]] = []  # (level, text) most recent per level
        self.last_heading = ""
        self.h2_history: list[str] = []
        self.text_lines: list[str] = []
        self.chrome_text_lines: list[str] = []
        self._buf: list[str] = []
        self.forms: list[dict] = []
        self.title = ""
        self._in_title = False
        self.selects: list[dict] = []
        self._select: dict | None = None

    # --- helpers
    def _chrome(self) -> bool:
        return any(t in _CHROME for t in self.stack)

    def _flush(self):
        line = _clean("".join(self._buf))
        self._buf = []
        if line:
            (self.chrome_text_lines if self._chrome() else self.text_lines).append(line)

    # --- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag in _SKIP:
            self.skip_depth += 1
        if tag not in _VOID:
            self.stack.append(tag)
        if self.skip_depth:
            return
        if tag in _BLOCK:
            self._flush()
        if tag in _HEADINGS:
            self.heading_buf = []
        if tag == "a":
            self.current_link = {"href": (a.get("href") or "").strip(), "text": [],
                                 "aria": a.get("aria-label") or a.get("title") or "",
                                 "section": self.last_heading,
                                 "plan_context": self.h2_history[-1] if self.h2_history else "",
                                 "chrome": self._chrome(), "has_href": "href" in a}
        if tag == "form":
            self.forms.append({"action": a.get("action"), "method": a.get("method"),
                               "name": a.get("name") or a.get("aria-label") or "",
                               "section": self.last_heading, "fields": []})
        if tag in ("input", "textarea", "select") and self.forms:
            name = a.get("name") or a.get("id") or ""
            if a.get("type") not in ("hidden", "submit", "button") and name:
                self.forms[-1]["fields"].append({"name": name, "type": a.get("type") or tag,
                                                 "placeholder": a.get("placeholder") or ""})
        if tag == "select":
            self._select = {"name": a.get("name") or a.get("id") or "", "options": []}
            self.selects.append(self._select)
        if tag == "option" and self._select is not None:
            self._select["options"].append({"value": a.get("value"), "label": ""})
        if tag == "img" and self.current_link is not None and a.get("alt"):
            self.current_link["text"].append(" " + a["alt"] + " ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self.skip_depth:
            self.skip_depth -= 1
        if not self.skip_depth:
            if tag in _HEADINGS and self.heading_buf is not None:
                text = _clean("".join(self.heading_buf))
                if text:
                    self.last_heading = text
                    if not self._chrome() and _PLAN_HEADING.match(text):
                        self.h2_history.append(text)
                self.heading_buf = None
            if tag == "a" and self.current_link is not None:
                link = self.current_link
                link["text"] = _clean("".join(link["text"])) or _clean(link.pop("aria", ""))
                link.pop("aria", None)
                self.links.append(link)
                self.current_link = None
            if tag == "select":
                self._select = None
            if tag in _BLOCK:
                self._flush()
        # pop to the matching tag (HTML is often unbalanced)
        if tag in self.stack:
            while self.stack:
                if self.stack.pop() == tag:
                    break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.skip_depth:
            return
        self._buf.append(data)
        if self.heading_buf is not None:
            self.heading_buf.append(data)
        if self.current_link is not None:
            self.current_link["text"].append(data)
        if self._select is not None and self._select["options"] and self.stack and self.stack[-1] == "option":
            self._select["options"][-1]["label"] += data.strip()


def extract(html: str) -> dict:
    p = _Extractor()
    p.feed(html)
    p.close()
    p._flush()

    def dedupe(lines):
        out = []
        for ln in lines:
            if not out or out[-1] != ln:
                out.append(ln)
        return out

    return {"title": _clean(p.title), "links": p.links, "text_lines": dedupe(p.text_lines),
            "chrome_lines": dedupe(p.chrome_text_lines), "forms": p.forms, "selects": p.selects}
