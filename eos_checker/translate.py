"""Chinese and English versions of every explanation, so the report can switch language.

The agents write their reasons in the --lang language, while regex messages and violation types come from
the rule book in English. After the run, each such text that exists in only one language is translated in a
few batched model calls (cached like every other call). Suggestions are never translated: they are rewrites
of the document's own English text.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable

from .llm import LLMClient, LLMError

LANGS = ("zh", "en")
BATCH = 20
_CJK = re.compile(r"[㐀-鿿]")
_TARGET = {"zh": "Simplified Chinese", "en": "English"}
_SYSTEM = """You translate short notes about English writing style into {target}.
The notes talk about English words and sentences, so:
- Text inside quotation marks ("...", '...', “...”, ‘...’) is English from the document or the style book. Copy it unchanged, in English, with its quotation marks. Never translate it.
- English words that are themselves the topic (data, while, very, less, fewer, its, it's ...) stay in English.
- Keep rule numbers, arrows (->) and symbols as they are.
- Translate everything else plainly. Do not add or drop information.
Example: {example}
Reply with only JSON: {{"results": [{{"i": <item number>, "text": "<translation>"}}]}}, one entry per item."""
_EXAMPLE = {
    "zh": "\"Data\" is a plural, like \"phenomena\". -> \"Data\" 是复数，和 \"phenomena\" 一样。",
    "en": "‘in a hasty manner’ 是多余的，应改为 ‘hastily’。 -> ‘in a hasty manner’ is wordy and should become ‘hastily’.",
}


def lang_of(text: str) -> str:
    return "zh" if _CJK.search(text) else "en"


def _other(lang: str) -> str:
    return "en" if lang == "zh" else "zh"


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”|‘([^’]+)’')


def _quoted(text: str) -> list[str]:
    """English fragments inside double or curly quotes (the words a note is talking about)."""
    out = []
    for m in _QUOTED.finditer(text):
        q = next(g for g in m.groups() if g is not None).strip()
        if re.search(r"[A-Za-z]", q) and not _CJK.search(q):
            out.append(q)
    return out


def _usable(src: str, text: str, target: str) -> bool:
    """Right language, and every quoted English fragment of the source survives word for word."""
    if not text or lang_of(text) != target:
        return False
    low = text.lower()
    return all(q.lower() in low for q in _quoted(src))


async def translate_all(llm: LLMClient | None, texts: Iterable[str],
                        known: dict[str, dict[str, str]] | None = None) -> tuple[dict[str, dict[str, str]], int]:
    """Return ({text: {"zh": ..., "en": ...}}, number of texts left untranslated).

    `known` holds ready-made pairs (e.g. a rule's English title and its hand-written Chinese name); those
    are used as they are. A text that cannot be translated (no model, failed call, unusable answer) keeps
    its original wording in both slots, so the report always has something to show.
    """
    table: dict[str, dict[str, str]] = {}
    todo: dict[str, list[str]] = {"zh": [], "en": []}
    for t in dict.fromkeys(x.strip() for x in texts if x and x.strip()):
        if known and t in known and all(known[t].get(k) for k in LANGS):
            table[t] = dict(known[t])
            continue
        src = lang_of(t)
        table[t] = {src: t}
        todo[_other(src)].append(t)

    async def one(target: str, items: list[str], hint: str = "") -> list[str]:
        """Translate a batch; return the items whose translation was missing or unusable."""
        user = "\n".join(f"{n}. {t}" for n, t in enumerate(items, 1))
        system = _SYSTEM.format(target=_TARGET[target], example=_EXAMPLE[target]) + hint
        try:
            data = await llm.complete_json(system, user, "results")
        except LLMError:
            return items
        for r in data["results"]:
            if not isinstance(r, dict):
                continue
            try:
                n = int(r.get("i"))
            except (TypeError, ValueError):
                continue
            text = str(r.get("text") or "").strip()
            if 1 <= n <= len(items) and _usable(items[n - 1], text, target):
                table[items[n - 1]][target] = text
        return [t for t in items if target not in table[t]]

    async def with_retry(target: str, items: list[str]) -> None:
        for t in await one(target, items):  # second try, one at a time, naming what must stay English
            keep = _quoted(t)
            hint = ("\nThe translation must contain these English fragments exactly as written (do not translate "
                    "them, and do not add this instruction to the translation): " + ", ".join(f'"{q}"' for q in keep)
                    ) if keep else ""
            await one(target, [t], hint)

    if llm is not None:
        jobs = [with_retry(target, items[i:i + BATCH])
                for target, items in todo.items() for i in range(0, len(items), BATCH)]
        await asyncio.gather(*jobs)

    missing = 0
    for t, d in table.items():
        for lang in LANGS:
            if lang not in d:
                d[lang] = t
                missing += 1
    return table, missing


def pick(table: dict[str, dict[str, str]] | None, text: str | None, lang: str) -> str:
    """The `lang` version of `text` (the text itself when there is none)."""
    t = (text or "").strip()
    return ((table or {}).get(t) or {}).get(lang, t)
