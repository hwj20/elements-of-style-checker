"""Turn a PDF (or plain-text file) into paragraphs and sentences.

For PDFs every character keeps its page and bounding box, so any span of text
found later (a quote reported by an agent) can be highlighted exactly in the
original file.

Handled here:
  * running headers / footers / page numbers are dropped
  * paragraphs broken across lines, columns and pages are rejoined
  * words hyphenated at line ends are rejoined (and every such division is
    recorded for the syllabication rule)
  * headings are recognised (bigger or bold font, short, no final stop)
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

Box = tuple  # (page, x0, y0, x1, y1)

_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "\t": " ",
})
_HYPHENS = {"-", "­", "‐", "‑"}
# Left parts that usually start a real compound ("well-known"), so the hyphen is kept.
_COMPOUND_LEFT = {
    "self", "well", "non", "ex", "all", "half", "high", "low", "long", "short", "cross",
    "full", "wide", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty",
    "ninety", "one", "two", "three", "four", "five", "first", "second", "third", "so",
    "anti", "pre", "post", "co", "re", "semi", "multi", "mid", "quasi", "pseudo", "over",
    "under", "much", "ill", "far", "near", "open", "close", "life", "time", "world",
    "state", "large", "small", "top", "end", "user", "data", "real", "on", "off",
}
_TERMINAL = re.compile(r'[.!?:]["\')\]]*$')


@dataclass
class Paragraph:
    pid: int
    text: str
    boxes: list | None  # one entry per character of text (None for inserted spaces)
    pages: list[int]
    is_heading: bool = False
    sent_ids: list[int] = field(default_factory=list)

    @property
    def page(self) -> int:
        return self.pages[0] if self.pages else 0


@dataclass
class Sentence:
    sid: int
    pid: int
    start: int  # offsets in the paragraph text
    end: int
    text: str
    index_in_para: int


@dataclass
class Hyphenation:
    hid: int
    page: int
    left: str
    right: str
    pid: int
    start: int  # offset of the joined word in the paragraph text
    end: int

    @property
    def word(self) -> str:
        return self.left + self.right


@dataclass
class Document:
    source: str
    kind: str  # pdf | text
    page_count: int
    pages_checked: list[int]
    paragraphs: list[Paragraph]
    sentences: list[Sentence]
    hyphenations: list[Hyphenation]
    dropped_lines: int = 0

    def sentence(self, sid: int) -> Sentence:
        return self.sentences[sid - 1]

    def paragraph(self, pid: int) -> Paragraph:
        return self.paragraphs[pid - 1]

    def page_of(self, pid: int, offset: int) -> int:
        p = self.paragraph(pid)
        if p.boxes:
            for i in range(min(offset, len(p.boxes) - 1), len(p.boxes)):
                if p.boxes[i] is not None:
                    return p.boxes[i][0]
            for i in range(min(offset, len(p.boxes) - 1), -1, -1):
                if p.boxes[i] is not None:
                    return p.boxes[i][0]
        return p.page

    def rects_for(self, pid: int, start: int, end: int) -> list[tuple[int, tuple]]:
        """Highlight rectangles (page, (x0, y0, x1, y1)) covering text[start:end]."""
        p = self.paragraph(pid)
        if not p.boxes:
            return []
        rects: list[list] = []  # [page, x0, y0, x1, y1]
        for b in p.boxes[max(0, start):min(end, len(p.boxes))]:
            if b is None:
                continue
            page, x0, y0, x1, y1 = b
            if rects:
                r = rects[-1]
                same_line = r[0] == page and abs((r[2] + r[4]) / 2 - (y0 + y1) / 2) < 0.5 * max(1.0, y1 - y0) and x0 >= r[1] - 1
                if same_line:
                    r[1], r[2], r[3], r[4] = min(r[1], x0), min(r[2], y0), max(r[3], x1), max(r[4], y1)
                    continue
            rects.append([page, x0, y0, x1, y1])
        return [(r[0], (r[1], r[2], r[3], r[4])) for r in rects]


# ----------------------------------------------------------------------------
# Sentence splitting
# ----------------------------------------------------------------------------

_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "v", "cf", "no", "nos", "vol", "vols",
    "pp", "p", "fig", "figs", "eq", "eqs", "al", "inc", "ltd", "co", "corp", "gen", "col", "capt",
    "lt", "sgt", "rev", "hon", "mt", "ft", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "approx", "ca", "viz", "ed", "eds", "ch", "chap", "sec", "art",
    "dept", "univ", "assn", "bros", "e.g", "i.e", "u.s", "u.k", "a.m", "p.m", "ph.d", "m.a", "b.a",
    "ibid", "op", "esp", "min", "max", "est", "govt", "messrs", "mme", "mlle",
}
_BOUNDARY = re.compile(r'([.!?]+)(["\')\]]*)(\s+)(?=["\'(\[]?[A-Z0-9])')
_LIST_MARK = re.compile(r'^\(?(?:\d{1,3}|[ivxIVX]{1,4}|[A-Za-z])\)?$')


def split_sentences(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        before = text[start:m.start(1)]
        if m.group(1) == ".":
            tok = re.search(r"([A-Za-z][A-Za-z.]*)$", before)
            if tok:
                t = tok.group(1).lower().rstrip(".")
                if t in _ABBREV or (len(t) == 1 and t.isalpha() and t != "i"):
                    continue
            if _LIST_MARK.match(before.strip()):  # "1. In 1788 ..." keep the marker with its sentence
                continue
        end = m.end(2)
        if text[start:end].strip():
            spans.append((start, end))
        start = m.end(3)
    if text[start:].strip():
        spans.append((start, len(text.rstrip())))
    # trim leading whitespace
    out = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        if s < e:
            out.append((s, e))
    return out


# ----------------------------------------------------------------------------
# PDF extraction
# ----------------------------------------------------------------------------

@dataclass
class _Line:
    chars: list  # [(char, (x0, y0, x1, y1))]
    page: int
    bbox: tuple
    size: float
    bold: bool


@dataclass
class _Block:
    page: int
    bbox: tuple
    lines: list
    page_height: float

    @property
    def text(self) -> str:
        return " ".join("".join(c for c, _ in ln.chars).strip() for ln in self.lines).strip()


class _Builder:
    """Accumulates paragraph text with a parallel list of character boxes."""

    def __init__(self, hyph_sink: list):
        self.chars: list[str] = []
        self.boxes: list = []
        self.pages: list[int] = []
        self.hyph_sink = hyph_sink  # (builder, left, right, start_offset, page)

    def text(self) -> str:
        return "".join(self.chars)

    def _push(self, c: str, box):
        c = c.translate(_QUOTE_MAP)
        if c in ("­",):
            return
        if c.isspace():
            if not self.chars or self.chars[-1] == " ":
                return
            c = " "
        self.chars.append(c)
        self.boxes.append(box)

    def add_line(self, line: _Line):
        chars = list(line.chars)
        # strip trailing spaces of the incoming line
        while chars and chars[-1][0].isspace():
            chars.pop()
        while chars and chars[0][0].isspace():
            chars.pop(0)
        if not chars:
            return
        if line.page not in self.pages:
            self.pages.append(line.page)
        if self.chars:
            # previous line ended with a hyphen and this one starts lower-case: rejoin the word
            while self.chars and self.chars[-1] == " ":
                self.chars.pop(); self.boxes.pop()
            last = self.chars[-1] if self.chars else ""
            first = chars[0][0]
            if last in _HYPHENS and len(self.chars) >= 2 and self.chars[-2].isalpha() and first.islower():
                # left fragment = letters before the hyphen
                i = len(self.chars) - 2
                while i >= 0 and (self.chars[i].isalpha() or self.chars[i] == "'"):
                    i -= 1
                left = "".join(self.chars[i + 1:-1])
                j = 0
                while j < len(chars) and (chars[j][0].isalpha() or chars[j][0] == "'"):
                    j += 1
                right = "".join(c for c, _ in chars[:j])
                keep_hyphen = left.lower() in _COMPOUND_LEFT
                if not keep_hyphen:
                    self.chars.pop(); self.boxes.pop()
                self.hyph_sink.append((self, left, right, i + 1, line.page, keep_hyphen))
            else:
                self._push(" ", None)
        for c, bb in chars:
            self._push(c, (line.page, *bb))


def _is_margin_noise(text: str) -> bool:
    t = text.strip().lower()
    return bool(re.fullmatch(r"(?:page\s*)?[\divxlc]{1,5}(?:\s*(?:of|/)\s*\d+)?|[-–—\s\d]+", t))


def _extract_pdf(path: Path, pages: list[int] | None) -> Document:
    import pymupdf

    doc = pymupdf.open(path)
    page_count = doc.page_count
    page_list = [p for p in (pages or range(page_count)) if 0 <= p < page_count]
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_LIGATURES & ~pymupdf.TEXT_PRESERVE_IMAGES

    blocks: list[_Block] = []
    sizes: Counter = Counter()
    for pno in page_list:
        page = doc[pno]
        h = page.rect.height
        d = page.get_text("rawdict", flags=flags)
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            lines = []
            for ln in b.get("lines", []):
                if abs(ln.get("dir", (1, 0))[1]) > 0.1:  # rotated / vertical text
                    continue
                chars, sz, n, bold_n = [], 0.0, 0, 0
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        chars.append((ch["c"], tuple(ch["bbox"])))
                    k = len(sp.get("chars", []))
                    sz += sp.get("size", 0) * k
                    n += k
                    if sp.get("flags", 0) & 16:  # bold
                        bold_n += k
                if not chars or not "".join(c for c, _ in chars).strip():
                    continue
                size = sz / n if n else 0
                sizes[round(size, 1)] += n
                lines.append(_Line(chars, pno, tuple(ln["bbox"]), size, bold_n > 0.6 * n))
            if lines:
                blocks.append(_Block(pno, tuple(b["bbox"]), lines, h))
    doc.close()

    body_size = sizes.most_common(1)[0][0] if sizes else 10.0

    # --- running heads / footers -------------------------------------------
    def norm(t: str) -> str:
        return re.sub(r"\d+", "#", t.strip().lower())

    margin = lambda b: b.bbox[3] < 0.09 * b.page_height or b.bbox[1] > 0.91 * b.page_height
    repeat = Counter(norm(b.text) for b in blocks if margin(b))
    min_repeat = max(2, int(0.3 * len(page_list)))
    kept, dropped = [], 0
    for b in blocks:
        if margin(b) and (_is_margin_noise(b.text) or (repeat[norm(b.text)] >= min_repeat and len(b.text) < 120)):
            dropped += 1
            continue
        kept.append(b)

    # --- paragraphs ----------------------------------------------------------
    hyph_raw: list = []
    paragraphs: list[tuple[_Builder, bool]] = []
    cur: _Builder | None = None
    cur_heading = False
    prev_block: _Block | None = None

    def is_heading(b: _Block) -> bool:
        t = b.text
        if len(t) > 140 or len(b.lines) > 3 or _TERMINAL.search(t) or t.endswith(","):
            return False
        avg = sum(l.size for l in b.lines) / len(b.lines)
        bold = all(l.bold for l in b.lines)
        caps = t.isupper() and len(t) > 3
        return avg >= body_size * 1.12 or bold or caps

    for b in kept:
        head = is_heading(b)
        merge = False
        if cur is not None and not head and not cur_heading and prev_block is not None:
            prev_text = cur.text().rstrip()
            first = b.text[:1]
            ends_open = not _TERMINAL.search(prev_text)
            if ends_open and (first.islower() or prev_text.endswith(tuple(_HYPHENS))):
                merge = True  # paragraph continues on the next block / column / page
            elif b.page == prev_block.page:
                last = prev_block.lines[-1]
                gap = b.lines[0].bbox[1] - last.bbox[3]
                aligned = abs(b.lines[0].bbox[0] - prev_block.lines[0].bbox[0]) < 3
                if aligned and gap < 0.35 * max(last.size, 1) and ends_open:
                    merge = True
        if not merge:
            if cur is not None:
                paragraphs.append((cur, cur_heading))
            cur = _Builder(hyph_raw)
            cur_heading = head
        for ln in b.lines:
            cur.add_line(ln)
        prev_block = b
    if cur is not None:
        paragraphs.append((cur, cur_heading))

    return _assemble(str(path), "pdf", page_count, page_list, paragraphs, hyph_raw, dropped)


def _assemble(source, kind, page_count, page_list, built, hyph_raw, dropped) -> Document:
    paras: list[Paragraph] = []
    sents: list[Sentence] = []
    index_of = {}
    for builder, heading in built:
        text = builder.text().strip()
        if not text:
            continue
        lead = len(builder.text()) - len(builder.text().lstrip())
        boxes = builder.boxes[lead:lead + len(text)] if builder.boxes is not None else None
        p = Paragraph(pid=len(paras) + 1, text=text, boxes=boxes, pages=list(builder.pages), is_heading=heading)
        index_of[id(builder)] = (p, lead)
        paras.append(p)
        if heading:
            continue
        for k, (s, e) in enumerate(split_sentences(text)):
            sent = Sentence(sid=len(sents) + 1, pid=p.pid, start=s, end=e, text=text[s:e], index_in_para=k)
            sents.append(sent)
            p.sent_ids.append(sent.sid)

    hyphs: list[Hyphenation] = []
    for builder, left, right, offset, page, keep in hyph_raw:
        if id(builder) not in index_of or not left or not right:
            continue
        p, lead = index_of[id(builder)]
        start = offset - lead
        end = start + len(left) + len(right) + (1 if keep else 0)
        if start < 0 or end > len(p.text):
            continue
        hyphs.append(Hyphenation(hid=len(hyphs) + 1, page=page, left=left, right=right, pid=p.pid, start=start, end=end))
    return Document(source=source, kind=kind, page_count=page_count, pages_checked=list(page_list),
                    paragraphs=paras, sentences=sents, hyphenations=hyphs, dropped_lines=dropped)


def _extract_text(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8", errors="replace").translate(_QUOTE_MAP)
    built = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        heading = text.startswith("#") or (
            len(lines) == 1 and len(text) < 80 and not _TERMINAL.search(text) and not text.endswith(","))
        if text.startswith("#"):
            text = text.lstrip("#").strip()
        b = _Builder([])
        b.chars = list(text)
        b.boxes = None
        built.append((b, heading))
    return _assemble(str(path), "text", 0, [], built, [], 0)


def load_document(path: str | Path, pages: list[int] | None = None) -> Document:
    """pages: 0-based page indexes to read (PDF only); None = all."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".pdf":
        return _extract_pdf(path, pages)
    return _extract_text(path)


def parse_page_range(spec: str | None) -> list[int] | None:
    """'1-5,8,10-12' (1-based, inclusive) -> 0-based list."""
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    return sorted(set(out))
