"""Run every selected rule agent over one document and write the reports."""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import report
from .agents import AgentOptions, AgentResult, Finding, RuleAgent
from .chunker import make_chunks
from .config import PROJECT_ROOT
from .document import load_document
from .llm import LLMClient, LLMConfig
from .rules import RuleBook
from .translate import translate_all


@dataclass
class CheckOptions:
    out_dir: Path
    pages: list[int] | None = None
    rule_ids: list[str] | None = None
    sections: list[str] | None = None
    include_outdated: bool = False
    chunk_chars: int = 2000
    concurrency: int = 16
    agent: AgentOptions = None  # type: ignore[assignment]
    use_cache: bool = True
    dry_run: bool = False
    quiet: bool = False


def _log(opts: CheckOptions, msg: str) -> None:
    if not opts.quiet:
        print(msg, file=sys.stderr, flush=True)


async def check_file(path: Path, book: RuleBook, llm_cfg: LLMConfig | None, opts: CheckOptions) -> dict:
    t0 = time.perf_counter()
    opts.agent = opts.agent or AgentOptions()
    doc = load_document(path, opts.pages)
    rules = book.select(opts.rule_ids, opts.sections, opts.include_outdated)
    chunks = make_chunks(doc, max_chars=opts.chunk_chars)
    _log(opts, f"[doc] {path.name}: {doc.page_count or '-'} pages, {len(doc.paragraphs)} paragraphs, "
               f"{len(doc.sentences)} sentences, {len(chunks)} chunks, {len(doc.hyphenations)} line-end divisions")
    if not doc.sentences:
        _log(opts, "[doc] no text found - is this a scanned PDF? (OCR it first)")

    llm = None
    if llm_cfg and opts.agent.use_llm and not opts.dry_run:
        cache = PROJECT_ROOT / ".cache" / "llm_cache.sqlite" if opts.use_cache else None
        llm = LLMClient(llm_cfg, concurrency=opts.concurrency, cache_path=cache)
    agents = [RuleAgent(r, doc, chunks, llm, opts.agent) for r in rules]

    if opts.dry_run:
        plans = {a.rule.id: a.plan() for a in agents}
        total = sum(p["llm_calls"] for p in plans.values())
        _log(opts, f"[plan] {len(agents)} agents, about {total} LLM calls")
        return {"plan": plans, "agents": len(agents), "llm_calls": total, "chunks": len(chunks)}

    _log(opts, f"[run] {len(agents)} agents (one per rule) reading the document from start to end ...")
    done = 0

    async def run_one(a: RuleAgent) -> AgentResult:
        nonlocal done
        res = await a.run()
        done += 1
        extra = f", review rejected {res.rejected_by_review}/{res.proposed}" if res.proposed else ""
        extra += f", {len(res.errors)} errors" if res.errors else ""
        _log(opts, f"  [{done:>3}/{len(agents)}] {a.rule.id:<22} {len(res.findings):>3} findings  "
                   f"({res.llm_calls} LLM calls, {res.seconds:.1f}s{extra})")
        return res

    results = await asyncio.gather(*(run_one(a) for a in agents))

    findings: list[Finding] = [f for r in results for f in r.findings]
    findings.sort(key=lambda f: (f.pid, f.start, f.rule_id))

    # Chinese + English for every explanation, so the report can switch language.
    by_id = {r.id: r for r in book.rules}
    texts = [f.reason for f in findings] + [report.violation_text(by_id[f.rule_id], f) for f in findings]
    calls_before = llm.stats.calls + llm.stats.cached if llm else 0
    known = {r.title.strip(): {"en": r.title.strip(), "zh": r.name_zh} for r in rules if r.name_zh}
    translations, untranslated = await translate_all(llm, texts, known)
    if llm and findings:
        _log(opts, f"[i18n] {len(translations)} explanations in Chinese and English "
                   f"({llm.stats.calls + llm.stats.cached - calls_before} LLM calls"
                   + (f", {untranslated} left untranslated" if untranslated else "") + ")")

    seconds = time.perf_counter() - t0
    run_info = {
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lang": opts.agent.lang,
        "untranslated": untranslated,
        "model": llm_cfg.model if llm else None,
        "seconds": round(seconds, 1),
        "llm_calls": llm.stats.calls if llm else 0,
        "llm_cached": llm.stats.cached if llm else 0,
        "prompt_tokens": llm.stats.prompt_tokens if llm else 0,
        "completion_tokens": llm.stats.completion_tokens if llm else 0,
        "llm_errors": llm.stats.errors if llm else 0,
        "min_confidence": opts.agent.min_confidence,
        "chunk_chars": opts.chunk_chars,
        "double_check": opts.agent.double_check,
        "agents": {r.rule_id: {"findings": len(r.findings), "llm_calls": r.llm_calls, "candidates": r.candidates,
                               "chunks_read": r.chunks_read, "chunks_skipped": r.chunks_skipped,
                               "scan_proposed": r.proposed, "rejected_by_review": r.rejected_by_review,
                               "dropped": r.dropped, "errors": r.errors[:5], "seconds": round(r.seconds, 1)}
                   for r in results},
    }

    out = opts.out_dir
    out.mkdir(parents=True, exist_ok=True)
    payload = report.build_payload(doc, book, rules, findings, run_info, translations)
    report.write_json(payload, out / "report.json")
    report.write_html(payload, out / "report.html")
    written = [out / "report.json", out / "report.html"]
    if doc.kind == "pdf":
        n = report.write_annotated_pdf(doc, findings, book, path, out / f"{path.stem}.annotated.pdf",
                                       lang=opts.agent.lang, translations=translations)
        written.append(out / f"{path.stem}.annotated.pdf")
        _log(opts, f"[pdf] {n} highlights placed")
    errors = sum(len(r.errors) for r in results)
    _log(opts, f"[done] {len(findings)} findings from {len(agents)} agents in {seconds:.0f}s; "
               f"{run_info['llm_calls']} LLM calls ({run_info['llm_cached']} from cache)"
               + (f"; {errors} failed calls - see report.json" if errors else ""))
    for w in written:
        _log(opts, f"       {w}")
    return payload
