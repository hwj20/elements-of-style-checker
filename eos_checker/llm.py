"""OpenAI-compatible chat client (vLLM / Qwen etc.) with JSON parsing, retries and a disk cache."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, BadRequestError


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout: float = 180.0


@dataclass
class LLMStats:
    calls: int = 0
    cached: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    errors: int = 0
    seconds: float = 0.0
    error_messages: list[str] = field(default_factory=list)


class _Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def get(self, key: str):
        row = self.db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_json_object(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences / <think>)."""
    if text is None:
        raise ValueError("empty reply")
    t = _THINK.sub("", text).strip()
    t = _FENCE.sub("", t).strip()
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                v = json.loads(t[start:i + 1])
                if isinstance(v, dict):
                    return v
                break
    raise ValueError("unterminated JSON object in reply")


class LLMClient:
    def __init__(self, cfg: LLMConfig, concurrency: int = 16, cache_path: Path | None = None):
        self.cfg = cfg
        self.client = AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout, max_retries=3)
        self.sem = asyncio.Semaphore(concurrency)
        self.cache = _Cache(cache_path) if cache_path else None
        self.stats = LLMStats()

    async def ping(self) -> list[str]:
        models = await self.client.models.list()
        return [m.id for m in models.data]

    async def complete_json(self, system: str, user: str, required_key: str) -> dict:
        """Ask for a JSON object that must contain `required_key` (a list)."""
        key = hashlib.sha256(json.dumps(
            [self.cfg.model, self.cfg.temperature, system, user], ensure_ascii=False).encode()).hexdigest()
        if self.cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.stats.cached += 1
                return hit

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        max_tokens = self.cfg.max_tokens
        last_err = ""
        for attempt in range(3):
            t0 = time.perf_counter()
            try:
                async with self.sem:
                    resp = await self.client.chat.completions.create(
                        model=self.cfg.model,
                        messages=messages,
                        temperature=self.cfg.temperature,
                        max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                    )
            except BadRequestError as e:  # e.g. prompt longer than the context window - retrying will not help
                self.stats.errors += 1
                raise LLMError(f"request rejected: {e.message}") from e
            except (APITimeoutError, APIConnectionError) as e:
                last_err = f"connection problem: {e}"
                self.stats.retries += 1
                await asyncio.sleep(2 * (attempt + 1))
                continue
            except APIStatusError as e:
                last_err = f"HTTP {e.status_code}: {e.message}"
                self.stats.retries += 1
                await asyncio.sleep(2 * (attempt + 1))
                continue
            finally:
                self.stats.seconds += time.perf_counter() - t0

            self.stats.calls += 1
            if resp.usage:
                self.stats.prompt_tokens += resp.usage.prompt_tokens or 0
                self.stats.completion_tokens += resp.usage.completion_tokens or 0
            choice = resp.choices[0]
            text = choice.message.content or ""
            try:
                data = parse_json_object(text)
                if not isinstance(data.get(required_key), list):
                    raise ValueError(f'reply has no "{required_key}" list')
                if self.cache:
                    self.cache.put(key, data)
                return data
            except (ValueError, json.JSONDecodeError) as e:
                last_err = f"bad JSON: {e}"
                self.stats.retries += 1
                if choice.finish_reason == "length":  # usually a repetition loop: one bigger retry only
                    max_tokens = min(max_tokens * 2, 4096)
                messages = messages[:2] + [
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": f'That reply was not usable ({e}). Answer again with ONLY the JSON object containing "{required_key}".'},
                ]
        self.stats.errors += 1
        if len(self.stats.error_messages) < 20:
            self.stats.error_messages.append(last_err)
        raise LLMError(last_err)
