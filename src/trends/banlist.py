"""Persistent banlist — every fingerprint we've EVER used is banned forever.

Categories tracked:
  topic         — exact story angles (one-sentence premises)
  hook          — opening line of every script (first 10 normalized words)
  title         — every YouTube title we have ever shipped
  number        — every dollar amount / percentage / large number used in a script
  name          — every character first-name used in a script
  scene_query   — every visual search query we ever sent to Pexels/Pixabay
  stock_clip_id — every Pexels/Pixabay clip ID we ever downloaded
  bamboo_action — every Veo action prompt we ever rendered for the Bamboo intro
  music_file    — every background-music file ever mixed into a final video

Policy:
- No TTL. Once shipped, banned forever.
- If a generation step cannot produce a value outside the banlist after N
  attempts, the pipeline SKIPS THE DAY rather than ship a clone.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.core.logging import get_logger
from src.core.paths import CACHE_TRENDS

log = get_logger(__name__)

BANLIST_DIR = CACHE_TRENDS / "banlists"
BANLIST_DIR.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()

CATEGORIES = (
    "topic", "hook", "title", "number", "name",
    "scene_query", "stock_clip_id", "bamboo_action", "music_file",
)


def _path(category: str) -> Path:
    if category not in CATEGORIES:
        raise ValueError(f"unknown banlist category: {category!r}")
    return BANLIST_DIR / f"{category}.json"


def _normalize(category: str, value: str) -> str:
    s = str(value).strip().lower()
    if category in ("topic", "hook", "title", "scene_query"):
        # Keep dollar and percent so "$340,000" remains distinct from "340"
        s = re.sub(r"[^\w\s$%.,-]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
    elif category == "name":
        s = re.sub(r"[^a-z]", "", s)
    elif category == "number":
        # Strip whitespace; preserve $ , . k m % so "$340,000" != "340000"
        s = re.sub(r"\s+", "", s)
    elif category == "stock_clip_id":
        s = re.sub(r"\s+", "", s)
    # bamboo_action and music_file are kept verbatim-but-lowercased + whitespace-collapsed
    elif category in ("bamboo_action", "music_file"):
        s = re.sub(r"\s+", " ", s).strip()
    return s


def _load(category: str) -> dict[str, str]:
    p = _path(category)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        log.warning("banlist %s unreadable, treating as empty: %s", category, e)
        return {}


def _save(category: str, data: dict[str, str]) -> None:
    _path(category).write_text(json.dumps(data, indent=2, sort_keys=True))


def is_banned(category: str, value: str) -> bool:
    n = _normalize(category, value)
    if not n:
        return False
    return n in _load(category)


def add(category: str, value: str) -> None:
    add_many(category, [value])


def add_many(category: str, values: Iterable[str]) -> int:
    """Append values to the banlist. Returns count of NEW entries added."""
    with _LOCK:
        data = _load(category)
        now = datetime.now(timezone.utc).isoformat()
        new_count = 0
        for v in values:
            n = _normalize(category, v)
            if not n:
                continue
            if n not in data:
                new_count += 1
            data[n] = now
        _save(category, data)
        if new_count:
            log.info("banlist[%s] +%d (total=%d)", category, new_count, len(data))
        return new_count


def list_recent(category: str, limit: int = 50) -> list[str]:
    """Return up to `limit` most-recently-added entries (normalized form)."""
    data = _load(category)
    items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in items[:limit]]


def size(category: str) -> int:
    return len(_load(category))


# Convenience: a single "fingerprint" call after each successful render so the
# orchestrator does not have to remember every category by hand.
def fingerprint_script(*, title: str, hook: str, topic: str,
                       numbers: Iterable[str], names: Iterable[str]) -> None:
    add("title", title)
    add("hook", hook)
    add("topic", topic)
    add_many("number", numbers)
    add_many("name", names)


def fingerprint_visuals(*, scene_queries: Iterable[str],
                        stock_clip_ids: Iterable[str],
                        bamboo_action: str | None = None,
                        music_file: str | None = None) -> None:
    add_many("scene_query", scene_queries)
    add_many("stock_clip_id", stock_clip_ids)
    if bamboo_action:
        add("bamboo_action", bamboo_action)
    if music_file:
        add("music_file", music_file)
