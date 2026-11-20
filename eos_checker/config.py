"""Settings from environment variables and an optional .env file."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env(extra: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env (current dir, then project root) without overriding real env vars."""
    for p in [extra, Path.cwd() / ".env", PROJECT_ROOT / ".env"]:
        if p and p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def llm_settings() -> dict:
    return {
        "base_url": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "model": os.environ.get("LLM_MODEL", "qwen3-vl-8b"),
    }
