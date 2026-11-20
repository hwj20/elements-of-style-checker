"""One agent per rule. Each agent walks the whole document from start to end.

An agent can do up to three things, depending on its rule entry:
  1. report  - regex triggers whose every match is a finding (no LLM);
  2. verify  - regex triggers whose matches are candidates the LLM confirms,
               sent in document order in small batches;
  3. scan    - the LLM reads every chunk of the document in order, looking
               only for this rule (optionally carrying notes forward). Every
               finding a scan proposes is then reviewed a second time by the
               same agent and kept only if the review confirms it.

Model answers are also discarded when they name no violation type of the rule,
when the "suggestion" is identical to the quoted text, or when the model's own
reason says nothing needs changing.
"""
from __future__ import annotations

import asyncio
import difflib
import re
import time
from dataclasses import asdict, dataclass, field

from . import prompts
from .chunker import Chunk
from .document import Document, Sentence
from .llm import LLMClient, LLMError
from .rules import Rule


@dataclass
class Finding:
    rule_id: str
    section: str
    severity: str
    source: str  # regex | llm-verify | llm-scan
    pid: int
    start: int  # offsets in the paragraph text
    end: int
    quote: str
    reason: str = ""
    suggestion: str = ""
    confidence: float = 1.0
    sid: int | None = None
    page: int = 0  # 0-based
    quote_found: bool = True
    scope: str = "sentence"
    vtype: int = 0  # which numbered violation type of the rule (0 = n/a)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentResult:
    rule_id: str
    findings: list[Finding] = field(default_factory=list)
    llm_calls: int = 0
    candidates: int = 0
    chunks_read: int = 0
    chunks_skipped: int = 0
    proposed: int = 0          # scan findings before the second review
    rejected_by_review: int = 0
    dropped: dict = field(default_factory=dict)  # reason -> count
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1


@dataclass
class AgentOptions:
    lang: str = "zh"
    min_confidence: float = 0.6
    verify_batch: int = 8
    hyphen_batch: int = 25
    window: int = 8  # chunks/batches of ONE agent in flight at once (1 = strictly sequential)
    use_llm: bool = True
    double_check: bool = True


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_NOOP_WORDS = re.compile(
    r"无需修改|不需要修改|无需更改|不需更改|无需改动|没有违反|未违反|不违反|并未违反|符合(?:该|此|本)?规则|不构成违规|"
    r"no change|not a violation|does not violate|doesn't violate|is correct as|no violation", re.I)


def find_span(hay: str, quote: str) -> tuple[int, int] | None:
    """Find `quote` in `hay`: exact, then case/whitespace-insensitive, then fuzzy."""
    q = quote.strip().strip('"').strip(prompts.MARK_OPEN + prompts.MARK_CLOSE).strip()
    q = q.replace(prompts.MARK_OPEN, "").replace(prompts.MARK_CLOSE, "")
    if not q:
        return None
    i = hay.find(q)
    if i >= 0:
        return i, i + len(q)
    norm, idx = [], []
    prev_space = False
    for k, c in enumerate(hay):
        if c.isspace():
            if prev_space:
                continue
            norm.append(" "); idx.append(k); prev_space = True
        else:
            norm.append(c.lower()); idx.append(k); prev_space = False
    hay_n = "".join(norm)
    q_n = _WS.sub(" ", q.lower())
    i = hay_n.find(q_n)
    if i >= 0:
        return idx[i], idx[i + len(q_n) - 1] + 1
    sm = difflib.SequenceMatcher(None, hay_n, q_n, autojunk=False)
    m = sm.find_longest_match(0, len(hay_n), 0, len(q_n))
    if m.size >= max(8, int(0.6 * len(q_n))):
        a, b = m.a, m.a + m.size
        while a > 0 and hay_n[a - 1].isalnum():
            a -= 1
        while b < len(hay_n) and hay_n[b].isalnum():
            b += 1
        return idx[a], idx[b - 1] + 1
    return None


_CJK = re.compile(r"[㐀-鿿]")
_PUNCT = set(',;:.!?()"—–')


def _same_text(a: str, b: str) -> bool:
    """Equal apart from whitespace and wrapping quotes / marks (punctuation and case DO count)."""
    def n(s: str) -> str:
        s = s.replace(prompts.MARK_OPEN, "").replace(prompts.MARK_CLOSE, "")
        return _WS.sub(" ", s).strip().strip("'").strip()
    return n(a) == n(b)


def _punct(s: str) -> str:
    return "".join(c for c in s if c in _PUNCT)


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "yes", "1"}


def _conf(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.7


async def _ordered(items, fn, window: int):
    """Run fn over items with at most `window` in flight; results keep document order."""
    sem = asyncio.Semaphore(max(1, window))

    async def one(x):
        async with sem:
            return await fn(x)

    return await asyncio.gather(*(one(x) for x in items))


# ----------------------------------------------------------------------------
# the agent
# ----------------------------------------------------------------------------

class RuleAgent:
    def __init__(self, rule: Rule, doc: Document, chunks: list[Chunk], llm: LLMClient | None, opts: AgentOptions):
        self.rule = rule
        self.doc = doc
        self.chunks = chunks
        self.llm = llm
        self.opts = opts
        self.res = AgentResult(rule_id=rule.id)

    @property
    def _llm_on(self) -> bool:
        return bool(self.opts.use_llm and self.llm)

    # -- building findings -------------------------------------------------------
    def _finding(self, source: str, s: Sentence | None, pid: int, start: int, end: int,
                 reason="", suggestion="", confidence=1.0, quote_found=True, scope="sentence", vtype=0) -> Finding:
        return Finding(
            rule_id=self.rule.id, section=self.rule.section, severity=self.rule.severity, source=source,
            pid=pid, start=start, end=end, quote=self.doc.paragraph(pid).text[start:end], reason=reason,
            suggestion=suggestion, confidence=confidence, sid=s.sid if s else None,
            page=self.doc.page_of(pid, start), quote_found=quote_found, scope=scope, vtype=vtype,
        )

    def _model_answer_ok(self, r: dict, quote: str, context: str) -> tuple[bool, int]:
        """Checks shared by every model-produced finding. `context` is the sentence (or paragraph).
        Returns (ok, violation type)."""
        vtype = 0
        if self.rule.violations:
            try:
                vtype = int(r.get("type", 0))
            except (TypeError, ValueError):
                vtype = 0
            if not 1 <= vtype <= len(self.rule.violations):
                self.res.drop("no valid violation type")
                return False, 0
        sugg, reason = str(r.get("suggestion", "")), str(r.get("reason", ""))
        if sugg and quote and _same_text(sugg, quote):
            self.res.drop("suggestion identical to text")
            return False, vtype
        if _NOOP_WORDS.search(reason) or _NOOP_WORDS.search(sugg):
            self.res.drop("model said no change needed")
            return False, vtype
        if self.rule.guard and not self.rule.guard.search(context):
            self.res.drop("sentence fails the rule's guard")
            return False, vtype
        tg = self.rule.type_guards.get(vtype)
        if tg and not tg.search(context):
            self.res.drop(f"sentence fails the guard of type {vtype}")
            return False, vtype
        if (vtype in self.rule.punctuation_fix_types and sugg and quote and not _CJK.search(sugg)
                and _punct(sugg) == _punct(quote)):
            self.res.drop("fix does not change punctuation")
            return False, vtype
        if _conf(r.get("confidence")) < self.opts.min_confidence:
            self.res.drop("low confidence")
            return False, vtype
        return True, vtype

    def _keep(self, f: Finding) -> None:
        self.res.findings.append(f)

    def _locate_in_sentence(self, s: Sentence, quote: str) -> tuple[int, int, bool]:
        span = find_span(s.text, quote) if quote else None
        if span:
            return s.start + span[0], s.start + span[1], True
        return s.start, s.end, False

    # -- 1 + 2: triggers ----------------------------------------------------------
    def _trigger_matches(self):
        """Emit report findings directly; return [(sentence, spans)] candidates to verify."""
        verify: dict[int, list[tuple[int, int]]] = {}
        for s in self.doc.sentences:
            n_in_para = len(self.doc.paragraph(s.pid).sent_ids)
            for t in self.rule.triggers:
                if t.first_sentence_only and s.index_in_para != 0:
                    continue
                if t.max_para_sentences and n_in_para > t.max_para_sentences:
                    continue
                if t.exclude and t.exclude.search(s.text):
                    continue
                for m in t.regex.finditer(s.text):
                    if m.end() == m.start():
                        continue
                    if t.action == "report":
                        msg = t.message or self.rule.title  # the book's own wording of the rule
                        self._keep(self._finding("regex", s, s.pid, s.start + m.start(), s.start + m.end(),
                                                 reason=msg, suggestion=t.suggestion))
                    else:
                        verify.setdefault(s.sid, []).append((m.start(), m.end()))
        # one candidate per (merged) match, so two problems in one sentence are judged separately
        out = []
        for sid, spans in sorted(verify.items()):
            merged: list[list[int]] = []
            for a, b in sorted(spans):
                if merged and a < merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            s = self.doc.sentence(sid)
            out += [(s, [(a, b)]) for a, b in merged]
        return out

    def _reasoning_consistent(self, r: dict) -> bool:
        """The model's own intermediate answers (think_fields) must support the verdict."""
        for k in self.rule.require_true:
            if not _truthy(r.get(k)):
                self.res.drop(f"reasoning field '{k}' is not true")
                return False
        for k in self.rule.require_false:
            if _truthy(r.get(k)):
                self.res.drop(f"reasoning field '{k}' is not false")
                return False
        return True

    async def _ask_sentences(self, cands, review: bool) -> list[tuple[Sentence, list, dict]]:
        """Send (sentence, spans) items in batches; return the items the model confirmed.
        A batch whose reply cannot be parsed is split in half and retried once."""
        system = prompts.verify_system(self.rule, self.opts.lang, review=review)
        batches = [cands[i:i + self.opts.verify_batch] for i in range(0, len(cands), self.opts.verify_batch)]

        para_ctx = self.rule.item_context == "paragraph"

        async def ask(batch):
            data = await self.llm.complete_json(
                system, prompts.render_verify_items(self.doc, batch, para_context=para_ctx), "results")
            return data["results"]

        async def run(batch):
            self.res.llm_calls += 1
            try:
                return [(batch, await ask(batch))], None
            except LLMError as e:
                if len(batch) < 2:
                    return [], str(e)
                half = len(batch) // 2
                out = []
                for part in (batch[:half], batch[half:]):
                    self.res.llm_calls += 1
                    try:
                        out.append((part, await ask(part)))
                    except LLMError as e2:
                        return out, str(e2)
                return out, None

        confirmed = []
        for parts, err in await _ordered(batches, run, self.opts.window):
            if err:
                self.res.errors.append(f"{'review' if review else 'verify'}: {err}")
            for batch, results in parts:
                for r in results:
                    if not isinstance(r, dict) or not _truthy(r.get("violation")):
                        continue
                    try:
                        n = int(r.get("item", 0))
                    except (TypeError, ValueError):
                        continue
                    if 1 <= n <= len(batch) and self._reasoning_consistent(r):
                        confirmed.append((batch[n - 1][0], batch[n - 1][1], r))
        return confirmed

    async def _verify(self, cands) -> None:
        self.res.candidates = len(cands)
        if not cands or not self._llm_on:
            return
        for s, spans, r in await self._ask_sentences(cands, review=False):
            quote = str(r.get("quote") or "")
            start, end, found = self._locate_in_sentence(s, quote)
            if not found:  # fall back to the span the pattern matched
                a, b = spans[0]
                start, end, found = s.start + a, s.start + b, True
            ok, vtype = self._model_answer_ok(r, self.doc.paragraph(s.pid).text[start:end], s.text)
            if ok:
                self._keep(self._finding("llm-verify", s, s.pid, start, end, reason=str(r.get("reason", "")),
                                         suggestion=str(r.get("suggestion", "")), confidence=_conf(r.get("confidence")),
                                         quote_found=found, vtype=vtype))

    # -- 3: scan ---------------------------------------------------------------
    def _chunk_selected(self, ch: Chunk) -> bool:
        if not self.rule.prefilter:
            return True
        text = " ".join(self.doc.sentence(s).text for s in ch.sids)
        return len(self.rule.prefilter.findall(text)) >= self.rule.prefilter_min

    def _proposals(self, ch: Chunk, violations: list) -> list[Finding]:
        sids = set(ch.sids)
        pids = {self.doc.sentence(s).pid for s in sids}
        para_rule = self.rule.scope == "paragraph"
        out: list[Finding] = []
        for v in violations:
            if not isinstance(v, dict):
                continue
            loc = str(v.get("loc", "")).strip().strip("[]¶ ").upper()
            quote = str(v.get("quote") or "")
            target: Sentence | None = None
            pid = None
            if loc.startswith("S") and loc[1:].isdigit() and int(loc[1:]) in sids:
                target = self.doc.sentence(int(loc[1:]))
                pid = target.pid
            elif loc.startswith("P") and loc[1:].isdigit() and int(loc[1:]) in pids:
                pid = int(loc[1:])
            else:  # label missing or outside the passage: look for the quote in the passage
                for sid in ch.sids:
                    s = self.doc.sentence(sid)
                    if quote and find_span(s.text, quote):
                        target, pid = s, s.pid
                        break
            if pid is None:
                self.res.drop("could not locate quote")
                continue
            if para_rule:
                p = self.doc.paragraph(pid)
                if self.rule.prefilter and len(self.rule.prefilter.findall(p.text)) < self.rule.prefilter_min:
                    self.res.drop("paragraph fails the rule's guard")
                    continue
                span = find_span(p.text, quote) if quote else None
                first = self.doc.sentence(p.sent_ids[0])
                start, end, found = (span[0], span[1], True) if span else (first.start, first.end, False)
                ok, vtype = self._model_answer_ok(v, p.text[start:end], p.text)
                if ok:
                    out.append(self._finding("llm-scan", None, pid, start, end, str(v.get("reason", "")),
                                             str(v.get("suggestion", "")), _conf(v.get("confidence")), found,
                                             scope="paragraph", vtype=vtype))
                continue
            if target is None:
                p = self.doc.paragraph(pid)
                target = next((self.doc.sentence(s) for s in p.sent_ids if s in sids
                               and quote and find_span(self.doc.sentence(s).text, quote)), None)
                if target is None:
                    self.res.drop("could not locate quote")
                    continue
            start, end, found = self._locate_in_sentence(target, quote)
            ok, vtype = self._model_answer_ok(v, self.doc.paragraph(pid).text[start:end], target.text)
            if ok:
                out.append(self._finding("llm-scan", target, pid, start, end, str(v.get("reason", "")),
                                         str(v.get("suggestion", "")), _conf(v.get("confidence")), found,
                                         scope=self.rule.scope, vtype=vtype))
        if para_rule:  # one finding per paragraph
            seen, uniq = set(), []
            for f in out:
                if f.pid not in seen:
                    seen.add(f.pid)
                    uniq.append(f)
            out = uniq
        if self.rule.max_per_chunk and len(out) > self.rule.max_per_chunk:
            out = sorted(out, key=lambda f: -f.confidence)[:self.rule.max_per_chunk]
        return out

    async def _scan(self) -> None:
        if not self._llm_on:
            return
        system = prompts.scan_system(self.rule, self.opts.lang)
        selected = [c for c in self.chunks if self._chunk_selected(c)]
        self.res.chunks_skipped = len(self.chunks) - len(selected)
        self.res.chunks_read = len(selected)
        proposals: list[Finding] = []

        if self.rule.carry_notes:  # strictly in order, passing notes along
            notes = ""
            for ch in selected:
                self.res.llm_calls += 1
                try:
                    data = await self.llm.complete_json(system, prompts.render_chunk(self.doc, ch, self.rule, notes), "violations")
                except LLMError as e:
                    self.res.errors.append(f"chunk {ch.cid}: {e}")
                    continue
                notes = str(data.get("notes") or notes)[:400]
                proposals += self._proposals(ch, data["violations"])
        else:
            async def run(ch):
                try:
                    data = await self.llm.complete_json(system, prompts.render_chunk(self.doc, ch, self.rule), "violations")
                    return ch, data["violations"], None
                except LLMError as e:
                    return ch, [], str(e)

            for ch, violations, err in await _ordered(selected, run, self.opts.window):
                self.res.llm_calls += 1
                if err:
                    self.res.errors.append(f"chunk {ch.cid}: {err}")
                    continue
                proposals += self._proposals(ch, violations)

        if self.rule.scope == "paragraph" and self.rule.prefilter:
            # paragraphs that meet the rule's pattern threshold are candidates even if the scan missed them
            have = {f.pid for f in proposals}
            for p in self.doc.paragraphs:
                if p.is_heading or p.pid in have or not p.sent_ids:
                    continue
                if len(self.rule.prefilter.findall(p.text)) >= self.rule.prefilter_min:
                    first = self.doc.sentence(p.sent_ids[0])
                    proposals.append(self._finding("llm-verify", None, p.pid, first.start, first.end,
                                                   confidence=1.0, quote_found=False, scope="paragraph"))

        self.res.proposed = len(proposals)
        if not proposals:
            return
        if not self.opts.double_check:
            for f in proposals:
                self._keep(f)
            return
        await self._review(proposals)

    async def _review(self, proposals: list[Finding]) -> None:
        """Second look: the same agent re-checks every finding its scan proposed."""
        if self.rule.scope == "paragraph":
            items = [(self.doc.paragraph(f.pid), (f.start, f.end) if f.quote_found else None) for f in proposals]
            system = prompts.verify_system(self.rule, self.opts.lang, review=True)
            batches = [list(range(i, min(i + 4, len(items)))) for i in range(0, len(items), 4)]

            async def run(idx):
                try:
                    data = await self.llm.complete_json(system, prompts.render_paragraph_items([items[i] for i in idx]), "results")
                    return idx, data["results"], None
                except LLMError as e:
                    return idx, [], str(e)

            answers: dict[int, dict] = {}
            for idx, results, err in await _ordered(batches, run, self.opts.window):
                self.res.llm_calls += 1
                if err:
                    self.res.errors.append(f"review: {err}")
                    continue
                for r in results:
                    if isinstance(r, dict) and _truthy(r.get("violation")) and self._reasoning_consistent(r):
                        try:
                            n = int(r.get("item", 0))
                        except (TypeError, ValueError):
                            continue
                        if 1 <= n <= len(idx):
                            answers[idx[n - 1]] = r
            for i, f in enumerate(proposals):
                r = answers.get(i)
                if r is None:
                    self.res.rejected_by_review += 1
                    continue
                p = self.doc.paragraph(f.pid)
                ok, vtype = self._model_answer_ok(r, f.quote, p.text)
                if not ok:
                    self.res.rejected_by_review += 1
                    continue
                f.reason = str(r.get("reason") or f.reason)
                f.suggestion = str(r.get("suggestion") or f.suggestion)
                f.confidence = min(f.confidence, _conf(r.get("confidence")))
                f.vtype = vtype or f.vtype
                self._keep(f)
            return

        cands, owner = [], []
        for f in proposals:
            s = self.doc.sentence(f.sid)
            cands.append((s, [(f.start - s.start, f.end - s.start)]))
            owner.append(f)
        by_sid = {}
        for s, spans, r in await self._ask_sentences(cands, review=True):
            by_sid.setdefault(s.sid, r)
        for f in owner:
            r = by_sid.get(f.sid)
            if r is None:
                self.res.rejected_by_review += 1
                continue
            ok, vtype = self._model_answer_ok(r, f.quote, self.doc.sentence(f.sid).text)
            if not ok:
                self.res.rejected_by_review += 1
                continue
            # the reviewer's wording is usually more focused
            f.reason = str(r.get("reason") or f.reason)
            f.suggestion = str(r.get("suggestion") or f.suggestion)
            f.confidence = min(f.confidence, _conf(r.get("confidence")))
            f.vtype = vtype or f.vtype
            self._keep(f)

    # -- hyphenation (line-end word division) -------------------------------------
    async def _hyphenation(self) -> None:
        items = list(self.doc.hyphenations)
        self.res.candidates = len(items)
        ask = []
        for h in items:
            if len(h.left) == 1 or len(h.right) == 1:  # "never cut off a single letter" - certain
                f = self._finding("regex", None, h.pid, h.start, h.end,
                                  reason="Divides off a single letter at the line end.",
                                  suggestion=f"do not divide '{h.word}' here")
                f.quote = f"{h.left}-{h.right}"
                self._keep(f)
            else:
                ask.append(h)
        if not ask or not self._llm_on:
            return
        system = prompts.hyphen_system(self.rule, self.opts.lang)
        batches = [ask[i:i + self.opts.hyphen_batch] for i in range(0, len(ask), self.opts.hyphen_batch)]

        async def run(batch):
            try:
                return batch, (await self.llm.complete_json(system, prompts.render_hyphen_items(batch), "results"))["results"], None
            except LLMError as e:
                return batch, [], str(e)

        for batch, results, err in await _ordered(batches, run, self.opts.window):
            self.res.llm_calls += 1
            if err:
                self.res.errors.append(f"hyphenation batch: {err}")
                continue
            for r in results:
                if not isinstance(r, dict) or r.get("violation") is not True:
                    continue
                try:
                    n = int(r.get("item", 0))
                except (TypeError, ValueError):
                    continue
                if not 1 <= n <= len(batch):
                    continue
                h = batch[n - 1]
                sugg = str(r.get("suggestion", ""))
                if sugg.replace(" ", "") == f"{h.left}-{h.right}":
                    self.res.drop("suggestion identical to text")
                    continue
                if _conf(r.get("confidence")) < self.opts.min_confidence:
                    self.res.drop("low confidence")
                    continue
                f = self._finding("llm-verify", None, h.pid, h.start, h.end, reason=str(r.get("reason", "")),
                                  suggestion=sugg, confidence=_conf(r.get("confidence")))
                f.quote = f"{h.left}-{h.right}"
                self._keep(f)

    # -- main --------------------------------------------------------------------
    async def run(self) -> AgentResult:
        t0 = time.perf_counter()
        if self.rule.scope == "hyphenation":
            await self._hyphenation()
        else:
            cands = self._trigger_matches()
            await self._verify(cands)
            if self.rule.scan:
                await self._scan()
        self._dedupe()
        self.res.seconds = time.perf_counter() - t0
        return self.res

    def _dedupe(self) -> None:
        """Same rule, same paragraph, overlapping spans -> keep the most confident one."""
        out: list[Finding] = []
        for f in sorted(self.res.findings, key=lambda f: (f.pid, f.start, -f.confidence)):
            dup = next((g for g in out if g.pid == f.pid and f.start < g.end and g.start < f.end), None)
            if dup is None:
                out.append(f)
            elif f.confidence > dup.confidence:
                out[out.index(dup)] = f
        self.res.findings = out

    # -- planning (dry run) ----------------------------------------------------------
    def plan(self) -> dict:
        """How much work this agent would do, without calling the model."""
        if self.rule.scope == "hyphenation":
            n = sum(1 for h in self.doc.hyphenations if len(h.left) > 1 and len(h.right) > 1)
            return {"report": len(self.doc.hyphenations) - n, "verify": n,
                    "llm_calls": -(-n // self.opts.hyphen_batch), "chunks": 0}
        saved, self.res = self.res, AgentResult(rule_id=self.rule.id)
        cands = self._trigger_matches()
        report = len(self.res.findings)
        self.res = saved
        calls = -(-len(cands) // self.opts.verify_batch)
        chunks = sum(1 for c in self.chunks if self._chunk_selected(c)) if self.rule.scan else 0
        return {"report": report, "verify": len(cands), "llm_calls": calls + chunks, "chunks": chunks}
