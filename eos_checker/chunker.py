"""Split a document into chunks that fit the model's context window.

A chunk is a run of whole sentences (paragraphs are kept together when they
fit). Each chunk also carries a little preceding text as read-only context, so
an agent reading chunk N still knows how chunk N-1 ended.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .document import Document


@dataclass
class Chunk:
    cid: int
    items: list[tuple[str, int]] = field(default_factory=list)  # ("heading", pid) | ("sent", sid)
    context_sids: list[int] = field(default_factory=list)
    chars: int = 0

    @property
    def sids(self) -> list[int]:
        return [i for k, i in self.items if k == "sent"]


def make_chunks(doc: Document, max_chars: int = 4500, context_chars: int = 400) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur = Chunk(cid=1)
    pending_headings: list[int] = []

    def close():
        nonlocal cur
        if cur.sids:
            chunks.append(cur)
            cur = Chunk(cid=len(chunks) + 1)

    for p in doc.paragraphs:
        if p.is_heading:
            pending_headings.append(p.pid)
            continue
        para_len = sum(len(doc.sentence(s).text) + 1 for s in p.sent_ids)
        # start a new chunk rather than split a paragraph that would fit in one
        if cur.sids and cur.chars + para_len > max_chars and para_len <= max_chars:
            close()
        for pid in pending_headings:
            cur.items.append(("heading", pid))
        pending_headings = []
        for sid in p.sent_ids:
            n = len(doc.sentence(sid).text) + 1
            if cur.sids and cur.chars + n > max_chars:
                close()
            cur.items.append(("sent", sid))
            cur.chars += n
    close()

    # read-only context: the sentences just before each chunk
    for ch in chunks:
        first = ch.sids[0]
        ctx, total = [], 0
        sid = first - 1
        while sid >= 1 and total < context_chars:
            ctx.insert(0, sid)
            total += len(doc.sentence(sid).text)
            sid -= 1
        ch.context_sids = ctx
    return chunks
