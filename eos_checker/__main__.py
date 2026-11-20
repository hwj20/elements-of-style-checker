"""Command line:  python -m eos_checker <command> ...

  check FILE.pdf     run the rule agents and write report.html / report.json / annotated PDF
  rules              list the rules (one agent each)
  extract FILE.pdf   show how the PDF was split into paragraphs / sentences / chunks
  ping               test the connection to the LLM server
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agents import AgentOptions
from .chunker import make_chunks
from .config import llm_settings, load_env
from .document import load_document, parse_page_range
from .llm import LLMClient, LLMConfig
from .rules import RuleError, load_rules
from .runner import CheckOptions, check_file


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _llm_config(args) -> LLMConfig:
    s = llm_settings()
    return LLMConfig(
        base_url=args.base_url or s["base_url"],
        api_key=args.api_key or s["api_key"] or "EMPTY",
        model=args.model or s["model"],
        max_tokens=args.max_tokens,
    )


def cmd_check(args) -> int:
    book = load_rules(args.rules_file)
    path = Path(args.file)
    out = Path(args.out) if args.out else path.with_name(path.stem + "_style_check")
    opts = CheckOptions(
        out_dir=out,
        pages=parse_page_range(args.pages),
        rule_ids=_csv(args.rules),
        sections=_csv(args.sections),
        include_outdated=args.include_outdated,
        chunk_chars=args.chunk_chars,
        concurrency=args.concurrency,
        agent=AgentOptions(lang=args.lang, min_confidence=args.min_confidence, window=args.window,
                           use_llm=not args.no_llm, double_check=not args.no_double_check),
        use_cache=not args.no_cache,
        dry_run=args.dry_run,
    )
    result = asyncio.run(check_file(path, book, None if args.no_llm else _llm_config(args), opts))
    if args.dry_run:
        rows = sorted(result["plan"].items(), key=lambda kv: -kv[1]["llm_calls"])
        print(f"{'rule':<24}{'regex hits':>11}{'to verify':>11}{'chunks':>8}{'LLM calls':>11}")
        for rid, p in rows:
            print(f"{rid:<24}{p['report']:>11}{p['verify']:>11}{p['chunks']:>8}{p['llm_calls']:>11}")
        print(f"\n{result['agents']} agents, {result['chunks']} chunks, about {result['llm_calls']} LLM calls in total")
    return 0


def cmd_rules(args) -> int:
    book = load_rules(args.rules_file)
    print(f"{'id':<22}{'status':<15}{'sev':<8}{'how':<28}name")
    for r in book.rules:
        if not args.all and r.status != "active":
            continue
        print(f"{r.id:<22}{r.status:<15}{r.severity:<8}{r.kind:<28}{r.name_zh}  |  {r.title[:70]}")
    active = sum(1 for r in book.rules if r.status == "active")
    print(f"\n{len(book.rules)} rules: {active} active"
          + ("" if args.all else " shown (use --all to include outdated / not applicable)"))
    return 0


def cmd_extract(args) -> int:
    doc = load_document(args.file, parse_page_range(args.pages))
    chunks = make_chunks(doc, max_chars=args.chunk_chars)
    if args.json:
        print(json.dumps({
            "paragraphs": [{"pid": p.pid, "pages": [x + 1 for x in p.pages], "heading": p.is_heading, "text": p.text}
                           for p in doc.paragraphs],
            "sentences": [{"sid": s.sid, "pid": s.pid, "text": s.text} for s in doc.sentences],
            "chunks": [{"cid": c.cid, "sentences": c.sids, "chars": c.chars} for c in chunks],
            "line_end_divisions": [{"page": h.page + 1, "left": h.left, "right": h.right} for h in doc.hyphenations],
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"{doc.page_count} pages, {len(doc.paragraphs)} paragraphs, {len(doc.sentences)} sentences, "
          f"{len(chunks)} chunks, {len(doc.hyphenations)} line-end divisions, {doc.dropped_lines} header/footer blocks dropped\n")
    for p in doc.paragraphs:
        pages = ",".join(str(x + 1) for x in p.pages)
        tag = "HEADING " if p.is_heading else ""
        print(f"P{p.pid} [p.{pages}] {tag}{p.text[:150]}{'...' if len(p.text) > 150 else ''}")
    return 0


def cmd_ping(args) -> int:
    cfg = _llm_config(args)

    async def go():
        c = LLMClient(cfg, concurrency=1)
        models = await c.ping()
        print(f"connected to {cfg.base_url}; models: {', '.join(models)}")
        data = await c.complete_json('Reply with JSON: {"results": ["ok"]}', "ping", "results")
        print(f"test call ok: {data}")

    try:
        asyncio.run(go())
    except Exception as e:  # noqa: BLE001 - show any connection problem plainly
        print(f"cannot reach the LLM at {cfg.base_url}: {e}", file=sys.stderr)
        print("Is the SSH tunnel open?  ssh -N -L 8000:127.0.0.1:8000 <user>@<cluster>", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    load_env()
    ap = argparse.ArgumentParser(prog="eos_checker", description="Check a PDF against The Elements of Style, one agent per rule.")
    ap.add_argument("--rules-file", help="rule book YAML (default: rules/elements_of_style_1918.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def llm_args(p):
        p.add_argument("--base-url", help="OpenAI-compatible endpoint (env LLM_BASE_URL)")
        p.add_argument("--api-key", help="API key (env LLM_API_KEY)")
        p.add_argument("--model", help="model name (env LLM_MODEL)")
        p.add_argument("--max-tokens", type=int, default=2048)

    c = sub.add_parser("check", help="check a PDF (or .txt/.md) file")
    c.add_argument("file")
    c.add_argument("-o", "--out", help="output folder (default: <file>_style_check next to the file)")
    c.add_argument("--pages", help="pages to check, 1-based, e.g. 1-20,25")
    c.add_argument("--rules", help="only these rule ids, e.g. II-05,III-10,V-etc")
    c.add_argument("--sections", help="only these sections, e.g. II,III")
    c.add_argument("--include-outdated", action="store_true", help="also run rules modern usage has abandoned")
    c.add_argument("--lang", default="zh", choices=["zh", "en"], help="language the agents write in and the annotated PDF uses (default zh); "
                        "report.html can switch between Chinese and English either way")
    c.add_argument("--min-confidence", type=float, default=0.6, help="drop model findings below this (default 0.6)")
    c.add_argument("--chunk-chars", type=int, default=2000, help="characters per chunk (default 2000, ~500 tokens; smaller = better recall with small models, more calls)")
    c.add_argument("--concurrency", type=int, default=16, help="LLM requests in flight across all agents")
    c.add_argument("--window", type=int, default=8, help="chunks one agent reads in parallel (1 = strictly in order)")
    c.add_argument("--no-llm", action="store_true", help="regex-only rules; no model calls")
    c.add_argument("--no-double-check", action="store_true",
                   help="keep scan findings without the second review (more findings, more false alarms)")
    c.add_argument("--no-cache", action="store_true", help="do not reuse cached model answers")
    c.add_argument("--dry-run", action="store_true", help="show how many model calls each agent would make")
    llm_args(c)
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("rules", help="list rules")
    r.add_argument("--all", action="store_true")
    r.set_defaults(fn=cmd_rules)

    e = sub.add_parser("extract", help="show the extracted paragraphs")
    e.add_argument("file")
    e.add_argument("--pages")
    e.add_argument("--chunk-chars", type=int, default=2000)
    e.add_argument("--json", action="store_true")
    e.set_defaults(fn=cmd_extract)

    p = sub.add_parser("ping", help="test the LLM connection")
    llm_args(p)
    p.set_defaults(fn=cmd_ping)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"error: file not found: {e}", file=sys.stderr)
        return 2
    except RuleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
