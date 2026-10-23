"""Story rotation — prevents repetitive content patterns.

Tracks THREE things to avoid repetition:
1. STORY TYPE — cautionary, success, comparison, system-exposed, journey
   → Never use the same type two days in a row
2. USED TOPICS — exact topic strings (already tracked by trending.py)
3. USED STOCK FOOTAGE — Pexels/Pixabay clip IDs
   → Skip clips used in the last 14 days

This is the difference between "AI slop" (same pattern daily) and a real
channel (varied content that keeps viewers guessing).
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.logging import get_logger
from src.core.paths import CACHE_TRENDS

log = get_logger(__name__)

ROTATION_FILE = CACHE_TRENDS / "story_rotation.json"
FOOTAGE_FILE = CACHE_TRENDS / "used_footage.json"

# Story types and their seed topic indices in trending.py
STORY_TYPES = ["cautionary", "success", "comparison", "system_exposed", "journey"]

# Map topic seeds to their type (index ranges in the SEED_QUERIES finance list)
TYPE_RANGES = {
    "cautionary": (0, 6),     # first 6 topics
    "success": (6, 11),       # next 5
    "comparison": (11, 14),   # next 3
    "system_exposed": (14, 18),  # next 4
    "journey": (18, 21),      # last 3
}


def _load_rotation() -> dict:
    if ROTATION_FILE.exists():
        try:
            return json.loads(ROTATION_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_rotation(data: dict) -> None:
    ROTATION_FILE.write_text(json.dumps(data, indent=2))


def get_next_story_type() -> str:
    """Returns the story type to use today. Never repeats the same type
    two days in a row. Cycles through all 5 types over a week."""
    data = _load_rotation()
    history = data.get("type_history", [])

    # Look at last 4 types to force cycling through ALL 5 before repeating
    recent = set(history[-4:]) if len(history) >= 4 else set(history)
    last_type = history[-1] if history else None

    # Find a type not used in the last 4 days AND not yesterday's
    chosen = None
    for t in STORY_TYPES:
        if t not in recent:
            chosen = t
            break

    # If all 5 types used in last 4 days, just avoid yesterday's
    if not chosen:
        for t in STORY_TYPES:
            if t != last_type:
                chosen = t
                break
        if not chosen:
            chosen = STORY_TYPES[0]

    # Save
    history.append(chosen)
    data["type_history"] = history[-10:]  # keep last 10
    data["last_date"] = datetime.now(timezone.utc).isoformat()
    _save_rotation(data)

    log.info("story type: %s (last was: %s)", chosen, last_type or "none")
    return chosen


def get_topic_range_for_type(story_type: str) -> tuple[int, int]:
    """Returns the (start, end) index range in SEED_QUERIES for this type."""
    return TYPE_RANGES.get(story_type, (0, 6))


# ---- Stock footage deduplication ----

def _load_used_footage() -> dict[str, str]:
    if FOOTAGE_FILE.exists():
        try:
            return json.loads(FOOTAGE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_used_footage(data: dict[str, str]) -> None:
    FOOTAGE_FILE.write_text(json.dumps(data, indent=2))


def mark_footage_used(clip_ids: list[str | int]) -> None:
    """Record that these clip IDs were used today."""
    data = _load_used_footage()
    now = datetime.now(timezone.utc).isoformat()
    for cid in clip_ids:
        data[str(cid)] = now
    # Prune entries older than 14 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    data = {k: v for k, v in data.items()
            if datetime.fromisoformat(v) >= cutoff}
    _save_used_footage(data)


def is_footage_used_recently(clip_id: str | int) -> bool:
    """Check if this clip was used in the last 14 days."""
    data = _load_used_footage()
    iso = data.get(str(clip_id))
    if not iso:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    return datetime.fromisoformat(iso) >= cutoff
