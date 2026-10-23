from __future__ import annotations
import os
from functools import lru_cache
from typing import Any
import yaml
from dotenv import load_dotenv
from .paths import ROOT, CONFIG

load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    with open(CONFIG / "settings.yaml") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def sources_allowlist() -> dict[str, Any]:
    with open(CONFIG / "sources_allowlist.yaml") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def prompts() -> dict[str, str]:
    with open(CONFIG / "prompts.yaml") as f:
        return yaml.safe_load(f)


def env(key: str, default: str | None = None, required: bool = False) -> str:
    v = os.getenv(key, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v or ""
