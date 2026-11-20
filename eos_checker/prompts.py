"""Prompt templates. Every prompt is about exactly one rule.

Small models over-report, so every prompt
  * numbers the kinds of violation the rule covers and makes the model name
    one ("type") - a finding that fits no listed type is discarded;
  * says explicitly that spelling slips and other rules' problems are not
    violations of this rule.
"""
from __future__ import annotations

from .chunker import Chunk
from .document import Document, Hyphenation, Paragraph, Sentence
from .rules import Rule

LANG_NAME = {"zh": "Simplified Chinese", "en": "English"}

MARK_OPEN, MARK_CLOSE = "⟦", "⟧"  # ⟦ ⟧


def rule_block(rule: Rule) -> str:
    lines = [
        "You are a careful copy editor. You check English prose against exactly ONE rule from "
        'William Strunk Jr.\'s "The Elements of Style" (1918). Other editors check every other rule, '
        "so spelling slips, typos and any problem not described below are NOT violations of this rule.",
        "",
        f"RULE {rule.id} ({rule.ref}): {rule.title}",
        "",
        "What the rule says:",
        rule.description,
    ]
    if rule.violations:
        lines += ["", "Violation types (report ONLY these):"]
        lines += [f"{i}. {v}" for i, v in enumerate(rule.violations, 1)]
    if rule.not_violations:
        lines += ["", "NOT violations (never report these):"] + [f"- {v}" for v in rule.not_violations]
    if rule.examples:
        lines += ["", "Examples (wrong -> right):"]
        for e in rule.examples:
            lines.append(f"- {e.get('bad', '')}  ->  {e.get('good', '')}")
    if rule.modern_note:
        lines += ["", f"Note on modern usage: {rule.modern_note}"]
    return "\n".join(lines)


def _fields(rule: Rule, lang: str, with_quote: bool = True) -> str:
    out = []
    if rule.violations:
        out.append(f'- "type": the number (1-{len(rule.violations)}) of the violation type it matches.')
    if with_quote:
        out.append('- "quote": the shortest span copied EXACTLY, character for character, from the text that shows the problem (2-25 words).')
    out += [
        f'- "reason": one short sentence in {LANG_NAME.get(lang, "English")} saying why it breaks this rule.',
        '- "suggestion": the corrected English wording of the quoted span. It must differ from the quote.',
        '- "confidence": a number from 0 to 1.',
    ]
    return "\n".join(out)


def _type_kv(rule: Rule) -> str:
    return '"type": 1, ' if rule.violations else ""


def scan_system(rule: Rule, lang: str) -> str:
    para = rule.scope == "paragraph"
    unit = "paragraph (label ¶P..)" if para else "sentence (label [S..])"
    notes = ""
    if rule.carry_notes:
        notes = ('\n- "notes": up to 40 words to remember for the NEXT passage (e.g. whether the text is '
                 "currently a summary and in which tense). Always include this key.")
    tail = ', "notes": "..."' if rule.carry_notes else ""
    shape = '{"violations": [{"loc": "%s", %s"quote": "...", "reason": "...", "suggestion": "...", "confidence": 0.9}]%s}' % (
        "P4" if para else "S12", _type_kv(rule), tail)
    limit = f"\n- Report at most {rule.max_per_chunk} items for the whole passage: only the clearest ones." if rule.max_per_chunk else ""
    return f"""{rule_block(rule)}

How to answer:
- The passage is split into sentences labelled [S12], [S13], ...; paragraphs start with ¶P4 lines; "###" lines are headings.
- Text under CONTEXT is only there so you understand the passage. Never report anything in it.
- Report only clear violations of THIS rule that match one of the numbered violation types. When in doubt, leave it out. Most passages contain no violation at all.{limit}
- "loc": the label of the {unit} that breaks the rule.
{_fields(rule, lang)}{notes}

Reply with ONLY this JSON object:
{shape}
If nothing in the passage breaks the rule, reply {{"violations": []{tail}}}"""


def render_chunk(doc: Document, chunk: Chunk, rule: Rule, notes: str = "") -> str:
    para_mode = rule.scope == "paragraph"
    out = []
    if notes:
        out += ["NOTES FROM EARLIER PASSAGES:", notes, ""]
    if chunk.context_sids:
        out.append("CONTEXT (already checked - do not report anything here):")
        out.append(" ".join(doc.sentence(s).text for s in chunk.context_sids))
        out.append("")
    out.append("PASSAGE TO CHECK:")
    in_chunk = set(chunk.sids)
    last_pid = None
    for kind, ident in chunk.items:
        if kind == "heading":
            out.append(f"### {doc.paragraph(ident).text}")
            last_pid = None
            continue
        s = doc.sentence(ident)
        if s.pid != last_pid:
            p = doc.paragraph(s.pid)
            label = f"¶P{p.pid}"
            if para_mode:
                n = len(p.sent_ids)
                label += f" ({n} sentence{'s' if n != 1 else ''})"
                if p.sent_ids[0] not in in_chunk:
                    label += " (continued from the previous passage)"
                if p.sent_ids[-1] not in in_chunk:
                    label += " (continues in the next passage)"
            out.append(label)
            last_pid = s.pid
        out.append(f"[S{s.sid}] {s.text}")
    return "\n".join(out)


def verify_system(rule: Rule, lang: str, review: bool = False) -> str:
    if review:
        task = (
            "Each ITEM below was flagged by a quick first reader as breaking this rule; the flagged words are marked "
            f"like {MARK_OPEN}this{MARK_CLOSE}. First readers are wrong about half the time. Check each item yourself "
            "against the rule text, the violation types and the NOT-violations, and answer true only if it is a clear "
            "violation of THIS rule."
        )
    else:
        task = (
            "Each ITEM below is one sentence in which a search pattern for this rule matched; the matched words are "
            f"marked like {MARK_OPEN}this{MARK_CLOSE}. Pattern matches are often false alarms. Decide whether the "
            "marked words really break THIS rule, judging by the rule text, violation types and examples above. "
            "Say false unless it is a clear violation."
        )
    if rule.verify_note:
        task += "\n" + rule.verify_note
    think = "".join(f'- "{k}": {v}\n' for k, v in rule.think_fields.items())
    think_kv = "".join(f'"{k}": "...", ' for k in rule.think_fields)
    if think:
        think = "Fill these in first, then decide:\n" + think
    return f"""{rule_block(rule)}

Your task:
{task}
Text given as "context" is only for understanding.

For each item:
- "item": the item number.
{think}- "violation": true or false.
If true, also give:
{_fields(rule, lang)}

Reply with ONLY this JSON object, one entry per item, in order:
{{"results": [{{"item": 1, {think_kv}"violation": true, {_type_kv(rule)}"quote": "...", "reason": "...", "suggestion": "...", "confidence": 0.9}}, {{"item": 2, {think_kv}"violation": false}}]}}"""


def _mark(text: str, spans: list[tuple[int, int]]) -> str:
    for a, b in sorted(set(spans), reverse=True):
        text = text[:a] + MARK_OPEN + text[a:b] + MARK_CLOSE + text[b:]
    return text


def render_verify_items(doc: Document, items: list[tuple[Sentence, list[tuple[int, int]]]],
                        para_context: bool = False) -> str:
    out = []
    for n, (s, spans) in enumerate(items, 1):
        out.append(f"ITEM {n}")
        if para_context:
            out.append(f"  context (the whole paragraph): {doc.paragraph(s.pid).text}")
        elif s.sid > 1:
            prev = doc.sentence(s.sid - 1)
            where = "context (previous sentence)" if prev.pid == s.pid else "context (end of the previous paragraph)"
            out.append(f"  {where}: {prev.text}")
        if s.index_in_para == 0 and len(doc.paragraph(s.pid).sent_ids) == 1:
            out.append("  (this sentence is a whole paragraph by itself)")
        out.append(f"  sentence: {_mark(s.text, spans)}")
    return "\n".join(out)


def render_paragraph_items(items: list[tuple[Paragraph, tuple[int, int] | None]]) -> str:
    out = []
    for n, (p, span) in enumerate(items, 1):
        k = len(p.sent_ids)
        text = _mark(p.text, [span]) if span else p.text
        out.append(f"ITEM {n} - a paragraph of {k} sentence{'s' if k != 1 else ''}:\n  {text}")
    return "\n".join(out)


def hyphen_system(rule: Rule, lang: str) -> str:
    return f"""{rule_block(rule)}

Your task:
Each ITEM is a word that the PDF divides at the end of a line. It is shown as left-part + "-" + right-part, together with the whole word.
Decide for each item whether the division breaks the principles above. If the hyphen belongs to a compound word (e.g. "well-known"), it is not a violation.

For each item:
- "item": the item number.
- "violation": true or false.
If true, also give:
- "reason": one short sentence in {LANG_NAME.get(lang, "English")} naming the principle that is broken.
- "suggestion": the better division, e.g. "know-ledge".
- "confidence": a number from 0 to 1.

Reply with ONLY this JSON object, one entry per item, in order:
{{"results": [{{"item": 1, "violation": true, "reason": "...", "suggestion": "know-ledge", "confidence": 0.9}}, {{"item": 2, "violation": false}}]}}"""


def render_hyphen_items(items: list[Hyphenation]) -> str:
    return "\n".join(f'ITEM {n}: {h.left}-{h.right}   (word: "{h.word}")' for n, h in enumerate(items, 1))
