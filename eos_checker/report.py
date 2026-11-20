"""Write the results: report.json, report.html and (for PDFs) an annotated copy of the PDF."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .agents import Finding
from .document import Document
from .rules import Rule, RuleBook
from .translate import pick

# Highlight colours in the annotated PDF, one per chapter of the book. Pale, cool tones on purpose:
# Acrobat also paints each comment's pop-up note in its highlight colour, and saturated or yellow
# tones make a long document tiring to read.
SECTION_RGB = {
    "II": (0.97, 0.76, 0.76),   # rose
    "III": (0.74, 0.83, 0.98),  # sky blue
    "IV": (0.71, 0.89, 0.89),   # aqua
    "V": (0.85, 0.79, 0.97),    # lavender
    "VI": (0.77, 0.92, 0.78),   # mint
}
_DEFAULT_RGB = (0.86, 0.87, 0.89)


def violation_text(r: Rule, f: Finding) -> str:
    return r.violations[f.vtype - 1] if 0 < f.vtype <= len(r.violations) else ""


def _context(doc: Document, f: Finding) -> tuple[str, int, int]:
    """The sentence (or paragraph start) around a finding and the quote's offsets within it."""
    p = doc.paragraph(f.pid)
    s = None
    for sid in p.sent_ids:
        cand = doc.sentence(sid)
        if cand.start <= f.start < cand.end + 1:
            s = cand
            break
    if s is None or f.scope == "paragraph" or f.end > s.end + 1:
        a, b = max(0, f.start - 160), min(len(p.text), f.end + 160)
    else:
        a, b = s.start, s.end
    return p.text[a:b], f.start - a, f.end - a


def build_payload(doc: Document, book: RuleBook, rules: list[Rule], findings: list[Finding], run_info: dict,
                  translations: dict[str, dict[str, str]] | None = None) -> dict:
    by_id = {r.id: r for r in book.rules}
    items = []
    for n, f in enumerate(findings, 1):
        r = by_id[f.rule_id]
        ctx, a, b = _context(doc, f)
        vt = violation_text(r, f)
        d = f.to_dict()
        d.update({
            "n": n,
            "page_label": f.page + 1 if doc.kind == "pdf" else None,
            "rule_title": r.title, "rule_name_zh": r.name_zh, "rule_ref": r.ref,
            "violation_type": vt,
            "reason_zh": pick(translations, f.reason, "zh"), "reason_en": pick(translations, f.reason, "en"),
            "violation_type_zh": pick(translations, vt, "zh"), "violation_type_en": pick(translations, vt, "en"),
            "context": ctx, "ctx_start": a, "ctx_end": b,
        })
        items.append(d)
    counts = {}
    for f in findings:
        counts[f.rule_id] = counts.get(f.rule_id, 0) + 1
    return {
        "document": {"source": doc.source, "kind": doc.kind, "pages": doc.page_count,
                     "pages_checked": [p + 1 for p in doc.pages_checked], "paragraphs": len(doc.paragraphs),
                     "sentences": len(doc.sentences), "line_end_divisions": len(doc.hyphenations)},
        "run": run_info,
        "rules": [{"id": r.id, "ref": r.ref, "section": r.section, "title": r.title, "name_zh": r.name_zh,
                   "severity": r.severity, "status": r.status, "kind": r.kind, "count": counts.get(r.id, 0),
                   **({"agent": run_info.get("agents", {}).get(r.id)} if run_info.get("agents") else {})}
                  for r in rules],
        "sections": book.meta.get("sections", {}),
        "findings": items,
    }


def write_json(payload: dict, path: Path) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_annotated_pdf(doc: Document, findings: list[Finding], book: RuleBook, src: Path, out: Path,
                        lang: str = "zh", translations: dict[str, dict[str, str]] | None = None) -> int:
    import pymupdf

    by_id = {r.id: r for r in book.rules}
    pdf = pymupdf.open(src)
    placed = 0
    for f in findings:
        rects = doc.rects_for(f.pid, f.start, f.end)
        if not rects:
            continue
        r = by_id[f.rule_id]
        color = SECTION_RGB.get(f.section, _DEFAULT_RGB)
        note = f"[{r.id}] {r.name_zh}\n{r.title}" if lang == "zh" else f"[{r.id}] {r.title}"
        reason = pick(translations, f.reason, lang)
        if reason:
            note += f"\n\n{reason}"
        if f.suggestion:
            note += f"\n-> {f.suggestion}"
        by_page: dict[int, list] = {}
        for page_no, rc in rects:
            by_page.setdefault(page_no, []).append(pymupdf.Rect(*rc))
        for page_no, rlist in by_page.items():
            page = pdf[page_no]
            if f.scope == "paragraph":
                first = rlist[0]
                a = page.add_text_annot(pymupdf.Point(max(4, first.x0 - 22), first.y0), note, icon="Comment")
                a.set_colors(stroke=color)
                a.set_info(title=r.id, subject=r.title)
                a.update()
            else:
                a = page.add_highlight_annot(rlist)
                a.set_colors(stroke=color)
                a.set_info(title=r.id, subject=r.title, content=note)
                a.update()
            placed += 1
    pdf.save(out, garbage=3, deflate=True)
    pdf.close()
    return placed


def write_html(payload: dict, path: Path) -> None:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(Path(payload["document"]["source"]).name)
    path.write_text(_HTML.replace("__TITLE__", title).replace("__DATA__", data), encoding="utf-8")


_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Style check - __TITLE__</title>
<style>
:root {
  color-scheme: light;
  --bg: #f5f4f0; --panel: #ffffff; --sunken: #f1efea; --text: #1e2024; --muted: #686d75; --faint: #9a9ea5;
  --line: #e7e4dd; --line-strong: #d6d2c9; --accent: #2f5f8f; --focus: rgba(47, 95, 143, .30);
  --s-II: #b8504a; --s-III: #3c6aa8; --s-IV: #2b7f86; --s-V: #7657ad; --s-VI: #3d8657;
  --t-II: rgba(184, 80, 74, .11); --t-III: rgba(60, 106, 168, .11); --t-IV: rgba(43, 127, 134, .11);
  --t-V: rgba(118, 87, 173, .11); --t-VI: rgba(61, 134, 87, .11);
  --high: #b8443b; --medium: #cf8c6b; --low: #a3a8b0;
  --shadow: 0 1px 2px rgba(30, 32, 36, .04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #151719; --panel: #1c1f22; --sunken: #232629; --text: #e4e3df; --muted: #a0a4aa; --faint: #6f747b;
    --line: #2a2e33; --line-strong: #383d43; --accent: #8db4e2; --focus: rgba(141, 180, 226, .35);
    --s-II: #e08a83; --s-III: #86a9dd; --s-IV: #6cbfc4; --s-V: #b096dc; --s-VI: #7cc495;
    --t-II: rgba(224, 138, 131, .15); --t-III: rgba(134, 169, 221, .15); --t-IV: rgba(108, 191, 196, .15);
    --t-V: rgba(176, 150, 220, .15); --t-VI: rgba(124, 196, 149, .15);
    --high: #e3837a; --medium: #d9a184; --low: #737981;
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #151719; --panel: #1c1f22; --sunken: #232629; --text: #e4e3df; --muted: #a0a4aa; --faint: #6f747b;
  --line: #2a2e33; --line-strong: #383d43; --accent: #8db4e2; --focus: rgba(141, 180, 226, .35);
  --s-II: #e08a83; --s-III: #86a9dd; --s-IV: #6cbfc4; --s-V: #b096dc; --s-VI: #7cc495;
  --t-II: rgba(224, 138, 131, .15); --t-III: rgba(134, 169, 221, .15); --t-IV: rgba(108, 191, 196, .15);
  --t-V: rgba(176, 150, 220, .15); --t-VI: rgba(124, 196, 149, .15);
  --high: #e3837a; --medium: #d9a184; --low: #737981;
  --shadow: none;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.6 "Segoe UI", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
button, input { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 72px; }

/* header */
.head { display: flex; gap: 16px; align-items: flex-start; justify-content: space-between; }
.head-actions { display: flex; gap: 8px; align-items: center; flex: none; }
.eyebrow { font-size: 12px; letter-spacing: .06em; color: var(--muted); text-transform: uppercase; }
h1 { font-size: 24px; line-height: 1.3; font-weight: 650; margin: 4px 0 6px; letter-spacing: -.01em; word-break: break-word; }
.meta { color: var(--muted); font-size: 13px; }
.meta span + span::before { content: "·"; margin: 0 8px; color: var(--faint); }
.icon-btn {
  flex: none; width: 36px; height: 36px; border-radius: 10px; border: 1px solid var(--line);
  background: var(--panel); color: var(--muted); display: grid; place-items: center;
}
.icon-btn:hover { color: var(--text); border-color: var(--line-strong); }
.icon-btn svg { width: 18px; height: 18px; }

/* summary */
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; box-shadow: var(--shadow); }
.summary { display: grid; grid-template-columns: auto 1fr auto; gap: 28px; align-items: center; padding: 18px 22px; margin: 22px 0 20px; }
.total b { display: block; font-size: 34px; line-height: 1; font-weight: 650; font-variant-numeric: tabular-nums; }
.total span { font-size: 13px; color: var(--muted); }
.sevbar { display: flex; height: 8px; border-radius: 99px; overflow: hidden; background: var(--sunken); gap: 2px; }
.sevbar span { min-width: 4px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 10px; font-size: 13px; color: var(--muted); }
.legend b { color: var(--text); font-weight: 600; margin-left: 4px; font-variant-numeric: tabular-nums; }
.swatch { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
.facts { display: flex; gap: 24px; margin: 0; }
.facts div { min-width: 64px; }
.facts dt { font-size: 12px; color: var(--muted); white-space: nowrap; }
.facts dd { margin: 2px 0 0; font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.facts small { font-size: 12px; font-weight: 400; color: var(--muted); margin-left: 4px; }
@media (max-width: 860px) {
  .summary { grid-template-columns: 1fr; gap: 16px; }
  .facts { flex-wrap: wrap; gap: 12px 24px; }
}

/* layout */
.layout { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 20px; align-items: start; }
@media (max-width: 860px) { .layout { grid-template-columns: minmax(0, 1fr); } }

/* rule list */
.rules { position: sticky; top: 16px; max-height: calc(100vh - 32px); display: flex; flex-direction: column; overflow: hidden; }
@media (max-width: 860px) { .rules { position: static; max-height: 46vh; } }
.rules-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 14px 16px 10px; border-bottom: 1px solid var(--line); }
.rules-head strong { font-size: 14px; font-weight: 600; }
.link-btn { border: 0; background: none; padding: 2px 4px; font-size: 12px; color: var(--accent); border-radius: 6px; }
.link-btn:hover { text-decoration: underline; }
.rules-body { overflow: auto; padding: 4px 0 8px; }
.sec-title { font-size: 12px; color: var(--muted); padding: 12px 16px 4px; display: flex; gap: 6px; align-items: baseline; white-space: nowrap; }
.sec-title b { color: var(--text); font-weight: 600; flex: none; }
.sec-title .zh { flex: none; }
.sec-title em { font-style: normal; color: var(--faint); overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.sec-title em.main { color: var(--muted); }
.rule-row {
  display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; gap: 10px; align-items: center;
  width: calc(100% - 12px); margin: 1px 6px; padding: 6px 10px; border: 0; border-radius: 9px;
  background: none; text-align: left; font-size: 13.5px; line-height: 1.35;
}
.rule-row:hover { background: var(--sunken); }
.rule-row.active { background: var(--t); }
.rule-row.active .nm { color: var(--c); font-weight: 600; }
.rule-row.zero { opacity: .5; }
.rule-row .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
.rule-row .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rule-row .id { display: block; font-size: 11.5px; color: var(--faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.count { min-width: 24px; padding: 0 7px; border-radius: 99px; background: var(--sunken); font-size: 12px; font-weight: 600; text-align: center; font-variant-numeric: tabular-nums; color: var(--muted); }
.rule-row.active .count { background: var(--panel); color: var(--c); }

/* toolbar */
.toolbar { display: flex; flex-wrap: wrap; gap: 10px 12px; align-items: center; padding: 10px 12px; margin-bottom: 14px; }
.search { position: relative; flex: 1 1 220px; }
.search svg { position: absolute; left: 10px; top: 50%; width: 16px; height: 16px; transform: translateY(-50%); color: var(--faint); pointer-events: none; }
.search input {
  width: 100%; padding: 7px 12px 7px 34px; border-radius: 10px; border: 1px solid var(--line);
  background: var(--bg); outline: none;
}
.search input:focus { border-color: var(--line-strong); box-shadow: 0 0 0 3px var(--focus); }
.seg { display: inline-flex; padding: 3px; gap: 2px; border-radius: 10px; background: var(--sunken); }
.seg button {
  border: 0; background: none; padding: 4px 10px; border-radius: 8px; font-size: 13px; color: var(--muted);
  display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
}
.seg button[aria-pressed="true"] { background: var(--panel); color: var(--text); box-shadow: 0 1px 2px rgba(0, 0, 0, .08); }
.seg button[aria-pressed="false"] .swatch { opacity: .35; }
.lang-seg button { padding: 5px 10px; }
.shown { font-size: 13px; color: var(--muted); margin-left: auto; white-space: nowrap; }
.filter-chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 6px 3px 10px; border-radius: 99px;
  background: var(--t); color: var(--c); font-size: 13px; border: 0; max-width: 100%;
}
.filter-chip .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filter-chip .x { font-size: 15px; line-height: 1; opacity: .7; }
.filter-chip:hover .x { opacity: 1; }

/* finding cards */
.card { padding: 16px 18px 14px; margin-bottom: 12px; }
.card-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 13px; }
.rid { padding: 1px 8px; border-radius: 6px; background: var(--t); color: var(--c); font-weight: 650; font-size: 12.5px; font-variant-numeric: tabular-nums; }
.rname { font-weight: 600; font-size: 14px; }
.spacer { flex: 1; }
.sev { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); }
.page { color: var(--muted); font-variant-numeric: tabular-nums; }
.ctx {
  margin: 12px 0 10px; padding: 2px 0 2px 14px; border-left: 2px solid var(--line-strong);
  font-family: "Iowan Old Style", "Palatino Linotype", Georgia, "Times New Roman", serif; font-size: 16px; line-height: 1.7;
  word-break: break-word;
}
mark {
  background: var(--t); color: inherit; padding: 1px 2px; border-radius: 3px;
  box-shadow: inset 0 -2px 0 var(--c);
  -webkit-box-decoration-break: clone; box-decoration-break: clone;
}
.reason { margin: 0; font-size: 14px; }
.sugg { display: flex; gap: 10px; align-items: baseline; margin-top: 10px; padding: 8px 12px; border-radius: 10px; background: var(--sunken); font-size: 14px; }
.sugg .lbl { flex: none; font-size: 12px; color: var(--muted); }
.sugg .txt { font-family: "Iowan Old Style", "Palatino Linotype", Georgia, serif; font-size: 15px; word-break: break-word; }
.foot { margin-top: 10px; font-size: 12px; color: var(--faint); }
.foot span + span::before { content: "·"; margin: 0 6px; }
.empty { color: var(--muted); padding: 40px 20px; text-align: center; }
.more { display: block; margin: 16px auto; padding: 7px 16px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); }
.more:hover { border-color: var(--line-strong); }
@media (max-width: 560px) {
  .wrap { padding: 20px 16px 56px; }
  .head { flex-direction: column-reverse; gap: 12px; }
  .head-actions { align-self: flex-end; }
  h1 { font-size: 20px; }
  .shown { margin-left: 0; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="head">
    <div>
      <div class="eyebrow" data-t="eyebrow"></div>
      <h1>__TITLE__</h1>
      <div class="meta" id="meta"></div>
    </div>
    <div class="head-actions">
      <div class="seg lang-seg" id="langSeg" role="group">
        <button data-lang="zh" lang="zh-CN">中文</button>
        <button data-lang="en" lang="en">English</button>
      </div>
      <button class="icon-btn" id="theme"></button>
    </div>
  </header>
  <section class="panel summary" id="summary"></section>
  <div class="layout">
    <aside class="panel rules">
      <div class="rules-head"><strong data-t="rules"></strong><button class="link-btn" id="toggleZero"></button></div>
      <div class="rules-body" id="rules"></div>
    </aside>
    <main>
      <div class="panel toolbar">
        <label class="search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
          <input type="search" id="q">
        </label>
        <div class="seg" id="sevSeg" role="group">
          <button data-sev="high" aria-pressed="true"><i class="swatch" style="background:var(--high)"></i><span></span></button>
          <button data-sev="medium" aria-pressed="true"><i class="swatch" style="background:var(--medium)"></i><span></span></button>
          <button data-sev="low" aria-pressed="true"><i class="swatch" style="background:var(--low)"></i><span></span></button>
        </div>
        <span id="chip"></span>
        <span class="shown" id="shown"></span>
      </div>
      <div id="list"></div>
    </main>
  </div>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const secVars = s => `--c:var(--s-${s}, var(--muted));--t:var(--t-${s}, var(--sunken))`;
const SEVS = ['high', 'medium', 'low'];

/* ---------- text in both languages ---------- */
const I18N = {
  zh: {
    eyebrow: 'The Elements of Style · 文体检查报告',
    pages: n => `${n} 页`, textFile: '文本文件', paras: n => `${n} 段`, sents: n => `${n} 句`,
    model: m => `模型 ${m}`, noModel: '未使用模型',
    findings: () => '个问题', sev: {high: '高', medium: '中', low: '低'},
    rulesChecked: '检查规则', calls: '模型调用', cached: n => `缓存 ${n}`, time: '耗时',
    rules: '规则', showAll: n => `显示全部 ${n} 条`, onlyHits: '只看有问题的', noFindings: '没有发现问题',
    search: '搜索原文、原因或规则', severity: '严重程度', lang: '语言',
    clearRule: '取消规则筛选', total: n => `共 ${n} 条`, shown: (a, b) => `显示 ${a} / ${b}`,
    nothing: '没有符合条件的问题', more: n => `再显示 300 条（还有 ${n} 条）`,
    page: n => `第 ${n} 页`, sugg: '建议',
    src: {'regex': '规则匹配', 'llm-verify': '模型确认', 'llm-scan': '模型通读'},
    theme: '切换深色 / 浅色',
  },
  en: {
    eyebrow: 'The Elements of Style · Style report',
    pages: n => n === 1 ? '1 page' : `${n} pages`, textFile: 'Text file',
    paras: n => `${n} paragraphs`, sents: n => `${n} sentences`,
    model: m => `Model ${m}`, noModel: 'No model',
    findings: n => n === 1 ? 'finding' : 'findings', sev: {high: 'High', medium: 'Medium', low: 'Low'},
    rulesChecked: 'Rules checked', calls: 'Model calls', cached: n => `${n} cached`, time: 'Time',
    rules: 'Rules', showAll: n => `Show all ${n}`, onlyHits: 'Only rules with findings', noFindings: 'No findings',
    search: 'Search text, reasons or rules', severity: 'Severity', lang: 'Language',
    clearRule: 'Clear rule filter', total: n => `${n} findings`, shown: (a, b) => `Showing ${a} of ${b}`,
    nothing: 'Nothing matches these filters', more: n => `Show 300 more (${n} left)`,
    page: n => `p. ${n}`, sugg: 'Suggestion',
    src: {'regex': 'Pattern match', 'llm-verify': 'Model check', 'llm-scan': 'Model read-through'},
    theme: 'Toggle dark mode',
  },
};
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};
let L = store.get('eos-lang');
if (!I18N[L]) L = I18N[D.run.lang] ? D.run.lang : 'zh';
const T = () => I18N[L];
const ruleName = r => L === 'zh' ? (r.name_zh || r.title) : r.title;
const reasonOf = f => (L === 'zh' ? f.reason_zh : f.reason_en) || f.reason || '';
const vtypeOf = f => (L === 'zh' ? f.violation_type_zh : f.violation_type_en) || f.violation_type || '';

const state = {rule: null, sev: new Set(SEVS), showZero: false, limit: 300};
const rulesById = Object.fromEntries(D.rules.map(r => [r.id, r]));

/* ---------- theme ---------- */
const root = document.documentElement;
const ICON = {
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
};
const isDark = () => root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
const paintThemeBtn = () => { $('theme').innerHTML = isDark() ? ICON.sun : ICON.moon; };
{ const t = store.get('eos-theme'); if (t === 'dark' || t === 'light') root.dataset.theme = t; }
$('theme').onclick = () => {
  root.dataset.theme = isDark() ? 'light' : 'dark';
  store.set('eos-theme', root.dataset.theme);
  paintThemeBtn();
};

/* ---------- static parts ---------- */
const doc = D.document, run = D.run;
const sevCount = {high: 0, medium: 0, low: 0};
D.findings.forEach(f => { sevCount[f.severity] = (sevCount[f.severity] || 0) + 1; });

function renderChrome() {
  const t = T();
  root.lang = L === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-t]').forEach(el => { el.textContent = t[el.dataset.t]; });
  $('q').placeholder = t.search;
  $('q').setAttribute('aria-label', t.search);
  $('sevSeg').setAttribute('aria-label', t.severity);
  $('langSeg').setAttribute('aria-label', t.lang);
  $('theme').title = t.theme;
  $('theme').setAttribute('aria-label', t.theme);
  document.querySelectorAll('#sevSeg button').forEach(b => { b.querySelector('span').textContent = t.sev[b.dataset.sev]; });
  document.querySelectorAll('#langSeg button').forEach(b => b.setAttribute('aria-pressed', b.dataset.lang === L));
  paintThemeBtn();

  $('meta').innerHTML = [
    doc.kind === 'pdf' ? t.pages(doc.pages) : t.textFile,
    t.paras(doc.paragraphs), t.sents(doc.sentences),
    run.model ? t.model(run.model) : t.noModel, run.finished_at || '',
  ].filter(Boolean).map(x => `<span>${esc(x)}</span>`).join('');

  const calls = (run.llm_calls ?? 0) + (run.llm_cached ?? 0);
  const secs = run.seconds ?? 0;
  $('summary').innerHTML = `
    <div class="total"><b>${D.findings.length}</b><span>${esc(t.findings(D.findings.length))}</span></div>
    <div>
      <div class="sevbar">${SEVS.filter(k => sevCount[k]).map(k =>
        `<span style="flex:${sevCount[k]};background:var(--${k})" title="${esc(t.sev[k])} ${sevCount[k]}"></span>`).join('')}</div>
      <div class="legend">${SEVS.map(k =>
        `<span><i class="swatch" style="background:var(--${k})"></i>${esc(t.sev[k])}<b>${sevCount[k]}</b></span>`).join('')}</div>
    </div>
    <dl class="facts">
      <div><dt>${esc(t.rulesChecked)}</dt><dd>${D.rules.length}</dd></div>
      <div><dt>${esc(t.calls)}</dt><dd>${calls}${run.llm_cached ? `<small>${esc(t.cached(run.llm_cached))}</small>` : ''}</dd></div>
      <div><dt>${esc(t.time)}</dt><dd>${secs < 1 ? '&lt;1' : secs.toFixed(0)} s</dd></div>
    </dl>`;
}

/* ---------- rule list ---------- */
function renderRules() {
  const t = T();
  const hidden = D.rules.filter(r => !r.count).length;
  $('toggleZero').textContent = state.showZero ? t.onlyHits : t.showAll(D.rules.length);
  $('toggleZero').hidden = !hidden;
  let html = '', lastSec = null;
  D.rules.forEach(r => {
    if (!r.count && !state.showZero && state.rule !== r.id) return;
    if (r.section !== lastSec) {
      lastSec = r.section;
      const s = D.sections[r.section] || {};
      html += L === 'zh'
        ? `<div class="sec-title" title="${esc(s.en || '')}"><b>${esc(r.section)}</b><span class="zh">${esc(s.zh || '')}</span><em>${esc(s.en || '')}</em></div>`
        : `<div class="sec-title" title="${esc(s.en || '')}"><b>${esc(r.section)}</b><em class="main">${esc(s.en || '')}</em></div>`;
    }
    html += `<button class="rule-row${r.count ? '' : ' zero'}${state.rule === r.id ? ' active' : ''}" data-id="${esc(r.id)}"
        style="${secVars(r.section)}" title="${esc(r.ref)}: ${esc(r.title)}">
      <span class="dot"></span>
      <span class="nm">${esc(ruleName(r))}<span class="id">${esc(r.id)} · ${esc(r.kind)}</span></span>
      <span class="count">${r.count}</span></button>`;
  });
  $('rules').innerHTML = html || `<div class="empty">${esc(t.noFindings)}</div>`;
}
$('rules').addEventListener('click', e => {
  const row = e.target.closest('.rule-row');
  if (!row) return;
  state.rule = state.rule === row.dataset.id ? null : row.dataset.id;
  state.limit = 300;
  render();
});
$('toggleZero').onclick = () => { state.showZero = !state.showZero; renderRules(); };

/* ---------- findings ---------- */
function ctxHtml(f) {
  const c = f.context, a = f.ctx_start, b = f.ctx_end;
  if (a < 0 || b > c.length || a >= b) return esc(c);
  return esc(c.slice(0, a)) + '<mark>' + esc(c.slice(a, b)) + '</mark>' + esc(c.slice(b));
}
function card(f) {
  const t = T();
  const how = (t.src[f.source] || f.source) + (f.source !== 'regex' && f.confidence != null ? ` ${Math.round(f.confidence * 100)}%` : '');
  const r = rulesById[f.rule_id] || {name_zh: f.rule_name_zh, title: f.rule_title};
  const reason = reasonOf(f), vt = vtypeOf(f);
  const ref = L === 'zh' ? `${f.rule_ref}: ${f.rule_title}` : f.rule_ref;
  return `<article class="panel card" style="${secVars(f.section)}">
    <div class="card-head">
      <span class="rid">${esc(f.rule_id)}</span>
      <span class="rname">${esc(ruleName(r))}</span>
      <span class="spacer"></span>
      <span class="sev"><i class="swatch" style="background:var(--${f.severity});margin:0"></i>${esc(t.sev[f.severity] || f.severity)}</span>
      ${f.page_label ? `<span class="page">${esc(t.page(f.page_label))}</span>` : ''}
    </div>
    <blockquote class="ctx" lang="en">${ctxHtml(f)}</blockquote>
    ${reason ? `<p class="reason">${esc(reason)}</p>` : ''}
    ${f.suggestion ? `<div class="sugg"><span class="lbl">${esc(t.sugg)}</span><span class="txt" lang="en">${esc(f.suggestion)}</span></div>` : ''}
    <div class="foot"><span>${esc(ref)}</span>${vt ? `<span>${esc(vt)}</span>` : ''}<span>${esc(how)}</span></div>
  </article>`;
}
function render() {
  const t = T();
  renderRules();
  const q = $('q').value.trim().toLowerCase();
  document.querySelectorAll('#sevSeg button').forEach(b => b.setAttribute('aria-pressed', state.sev.has(b.dataset.sev)));
  const rule = rulesById[state.rule];
  $('chip').innerHTML = rule
    ? `<button class="filter-chip" id="clearRule" style="${secVars(rule.section)}" title="${esc(t.clearRule)}" aria-label="${esc(t.clearRule)}"><span class="lbl">${esc(rule.id)} ${esc(ruleName(rule))}</span><span class="x">×</span></button>`
    : '';
  if (rule) $('clearRule').onclick = () => { state.rule = null; render(); };
  const rows = D.findings.filter(f => state.sev.has(f.severity) && (!state.rule || f.rule_id === state.rule) &&
    (!q || [f.context, f.reason, f.reason_zh, f.reason_en, f.suggestion, f.rule_title, f.rule_name_zh, f.rule_id]
      .join(' ').toLowerCase().includes(q)));
  $('shown').textContent = rows.length === D.findings.length ? t.total(rows.length) : t.shown(rows.length, D.findings.length);
  const list = $('list');
  if (!rows.length) {
    list.innerHTML = `<div class="panel empty">${esc(D.findings.length ? t.nothing : t.noFindings)}</div>`;
    return;
  }
  list.innerHTML = rows.slice(0, state.limit).map(card).join('') +
    (rows.length > state.limit ? `<button class="more" id="more">${esc(t.more(rows.length - state.limit))}</button>` : '');
  if (rows.length > state.limit) $('more').onclick = () => { state.limit += 300; render(); };
}
$('q').addEventListener('input', () => { state.limit = 300; render(); });
document.querySelectorAll('#sevSeg button').forEach(b => b.addEventListener('click', () => {
  const k = b.dataset.sev;
  if (state.sev.has(k)) state.sev.delete(k); else state.sev.add(k);
  state.limit = 300;
  render();
}));
document.querySelectorAll('#langSeg button').forEach(b => b.addEventListener('click', () => {
  if (b.dataset.lang === L) return;
  L = b.dataset.lang;
  store.set('eos-lang', L);
  renderChrome();
  render();
}));
renderChrome();
render();
</script>
</body>
</html>
"""
