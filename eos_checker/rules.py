"""Load the rule book (YAML), validate it and compile its regular expressions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "elements_of_style_1918.yaml"

SCOPES = {"sentence", "paragraph", "hyphenation"}
STATUSES = {"active", "outdated", "not_applicable"}
SEVERITIES = {"high", "medium", "low"}
ACTIONS = {"report", "verify"}


class RuleError(ValueError):
    pass


@dataclass
class Trigger:
    pattern: str
    regex: re.Pattern
    action: str  # report | verify
    exclude: re.Pattern | None = None
    first_sentence_only: bool = False
    max_para_sentences: int = 0  # >0: only in paragraphs with at most this many sentences
    message: str = ""
    suggestion: str = ""


@dataclass
class Rule:
    id: str
    ref: str
    section: str
    title: str
    name_zh: str
    severity: str
    status: str
    scope: str
    description: str
    violations: list[str] = field(default_factory=list)
    not_violations: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    scan: bool = False
    prefilter: re.Pattern | None = None
    prefilter_min: int = 1
    carry_notes: bool = False
    modern_note: str = ""
    verify_note: str = ""  # extra instruction shown when the LLM verifies trigger candidates
    think_fields: dict = field(default_factory=dict)  # {name: instruction} filled in BEFORE "violation"
    item_context: str = "sentence"  # context shown with verify/review items: previous "sentence" or whole "paragraph"
    require_true: list = field(default_factory=list)   # think fields that must be true for a finding to count
    require_false: list = field(default_factory=list)  # think fields that must be false for a finding to count
    max_per_chunk: int = 0  # >0: keep at most this many scan findings per chunk (subjective rules)
    # Deterministic checks on model findings (small models invent problems):
    guard: re.Pattern | None = None  # the sentence must match this for any model finding
    type_guards: dict = field(default_factory=dict)  # {violation type: pattern the sentence must match}
    punctuation_fix_types: list = field(default_factory=list)  # types whose fix must change punctuation

    @property
    def kind(self) -> str:
        """Human label for how this agent works."""
        if self.scope == "hyphenation":
            return "llm"
        parts = []
        if any(t.action == "report" for t in self.triggers):
            parts.append("regex")
        if any(t.action == "verify" for t in self.triggers):
            parts.append("regex+llm")
        if self.scan:
            parts.append("llm-scan")
        return " / ".join(parts) or "none"

    @property
    def uses_llm(self) -> bool:
        return self.scan or self.scope == "hyphenation" or any(t.action == "verify" for t in self.triggers)


@dataclass
class RuleBook:
    meta: dict
    rules: list[Rule]

    def section_name(self, sec: str, lang: str = "en") -> str:
        return self.meta.get("sections", {}).get(sec, {}).get(lang, sec)

    def select(self, ids: list[str] | None = None, sections: list[str] | None = None,
               include_outdated: bool = False) -> list[Rule]:
        chosen = []
        id_set = {i.strip().lower() for i in ids} if ids else None
        sec_set = {s.strip().upper() for s in sections} if sections else None
        for r in self.rules:
            if r.status == "not_applicable":
                continue
            if id_set is not None:
                # explicit selection runs the rule even if it is outdated
                if r.id.lower() not in id_set:
                    continue
            else:
                if r.status == "outdated" and not include_outdated:
                    continue
                if sec_set is not None and r.section.upper() not in sec_set:
                    continue
            chosen.append(r)
        if id_set is not None:
            known = {r.id.lower() for r in self.rules}
            unknown = sorted(id_set - known)
            if unknown:
                raise RuleError(f"Unknown rule id(s): {', '.join(unknown)}")
        return chosen


def _compile(pattern: str, case_sensitive: bool, where: str) -> re.Pattern:
    try:
        return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        raise RuleError(f"{where}: bad regex {pattern!r}: {e}") from e


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def load_rules(path: str | Path | None = None) -> RuleBook:
    path = Path(path) if path else DEFAULT_RULES_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "rules" not in data:
        raise RuleError(f"{path}: expected a mapping with a 'rules' list")
    rules: list[Rule] = []
    seen: set[str] = set()
    for i, raw in enumerate(data["rules"]):
        rid = str(raw.get("id") or f"#{i}")
        where = f"rule {rid}"
        if rid in seen:
            raise RuleError(f"{where}: duplicate id")
        seen.add(rid)
        for key in ("section", "title", "scope"):
            if not raw.get(key):
                raise RuleError(f"{where}: missing '{key}'")
        scope = raw["scope"]
        status = raw.get("status", "active")
        severity = raw.get("severity", "medium")
        if scope not in SCOPES:
            raise RuleError(f"{where}: scope must be one of {sorted(SCOPES)}")
        if status not in STATUSES:
            raise RuleError(f"{where}: status must be one of {sorted(STATUSES)}")
        if severity not in SEVERITIES:
            raise RuleError(f"{where}: severity must be one of {sorted(SEVERITIES)}")

        triggers: list[Trigger] = []
        for j, t in enumerate(_as_list(raw.get("triggers"))):
            tw = f"{where} trigger {j + 1}"
            action = t.get("action", "verify")
            if action not in ACTIONS:
                raise RuleError(f"{tw}: action must be report or verify")
            cs = bool(t.get("case_sensitive", False))
            triggers.append(Trigger(
                pattern=t["pattern"],
                regex=_compile(t["pattern"], cs, tw),
                action=action,
                exclude=_compile(t["exclude"], cs, tw + " exclude") if t.get("exclude") else None,
                first_sentence_only=bool(t.get("first_sentence_only", False)),
                max_para_sentences=int(t.get("max_para_sentences", 0)),
                message=t.get("message", ""),
                suggestion=t.get("suggestion", ""),
            ))
        # A misspelling table becomes one "report" trigger per wrong form.
        for wrong, right in (raw.get("misspellings") or {}).items():
            cs = wrong != wrong.lower() or wrong in {"shepard"}  # Shepard is also a surname
            triggers.append(Trigger(
                pattern=rf"\b{re.escape(wrong)}\b",
                regex=_compile(rf"\b{re.escape(wrong)}\b", cs, f"{where} misspelling {wrong}"),
                action="report",
                message=f"Misspelled: '{wrong}' -> '{right}'.",
                suggestion=right,
            ))

        prefilter = raw.get("prefilter")
        rule = Rule(
            id=rid,
            ref=str(raw.get("ref", "")),
            section=str(raw["section"]),
            title=str(raw["title"]).strip(),
            name_zh=str(raw.get("name_zh", "")),
            severity=severity,
            status=status,
            scope=scope,
            description=" ".join(str(raw.get("description", "")).split()),
            violations=[str(v) for v in _as_list(raw.get("violations"))],
            not_violations=[str(v) for v in _as_list(raw.get("not_violations"))],
            examples=[e for e in _as_list(raw.get("examples")) if isinstance(e, dict)],
            triggers=triggers,
            scan=bool(raw.get("scan", False)),
            prefilter=_compile(prefilter, False, where + " prefilter") if prefilter else None,
            prefilter_min=int(raw.get("prefilter_min", 1)),
            carry_notes=bool(raw.get("carry_notes", False)),
            modern_note=str(raw.get("modern_note", "")),
            verify_note=" ".join(str(raw.get("verify_note", "")).split()),
            think_fields={str(k): " ".join(str(v).split()) for k, v in (raw.get("think_fields") or {}).items()},
            item_context=str(raw.get("item_context", "sentence")),
            require_true=[str(x) for x in _as_list(raw.get("require_true"))],
            require_false=[str(x) for x in _as_list(raw.get("require_false"))],
            max_per_chunk=int(raw.get("max_per_chunk", 0)),
            guard=_compile(raw["guard"], False, where + " guard") if raw.get("guard") else None,
            type_guards={int(k): _compile(v, False, f"{where} type_guard {k}")
                         for k, v in (raw.get("type_guards") or {}).items()},
            punctuation_fix_types=[int(x) for x in _as_list(raw.get("punctuation_fix_types"))],
        )
        for k in rule.require_true + rule.require_false:
            if k not in rule.think_fields:
                raise RuleError(f"{where}: require_true/require_false names '{k}', which is not in think_fields")
        for k in list(rule.type_guards) + rule.punctuation_fix_types:
            if not 1 <= k <= len(rule.violations):
                raise RuleError(f"{where}: guard refers to violation type {k}, but there are {len(rule.violations)} types")
        if status != "not_applicable" and not (rule.scan or rule.triggers or scope == "hyphenation"):
            raise RuleError(f"{where}: has neither triggers nor scan: true - nothing would check it")
        rules.append(rule)
    return RuleBook(meta=data.get("meta", {}), rules=rules)
