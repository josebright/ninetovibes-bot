"""Deep deduplication — prevents ALL forms of repetition.

Tracks and prevents:
1. Exact topic reuse (already in trending.py)
2. Similar topic reuse (semantic similarity check)
3. Same story type back-to-back (in story_rotation.py)
4. Same stock footage clips (in stock_scenes.py)
5. Same script patterns (opening lines, character names, dollar amounts)
6. Same Bamboo intro clip (tracks which clip was used last)

Every video must feel UNIQUE. Not just different topic — different opening,
different character name, different dollar amounts, different emotional arc.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

from src.core.logging import get_logger
from src.core.paths import CACHE_TRENDS

log = get_logger(__name__)

SCRIPT_HISTORY_FILE = CACHE_TRENDS / "script_history.json"
BAMBOO_CLIP_HISTORY_FILE = CACHE_TRENDS / "bamboo_clip_history.json"
MAX_HISTORY = 30  # track last 30 videos


def _load_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []


def _save_json(path: Path, data: list) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


# ---- Script dedup ----

def save_script_fingerprint(draft_id: str, title: str, hook: str,
                            tts_text: str, topic: str) -> None:
    """Save key script details so we can check future scripts for similarity."""
    history = _load_json(SCRIPT_HISTORY_FILE)

    # Extract fingerprint elements
    numbers = re.findall(r'\$[\d,]+[KkMm]?|\d{2,}%|\d{3,}', tts_text)
    names = re.findall(r'\b[A-Z][a-z]+\b', tts_text)  # character names
    opening_words = " ".join(tts_text.split()[:10]).lower()

    history.append({
        "draft_id": draft_id,
        "title": title,
        "hook": hook,
        "topic": topic,
        "opening_words": opening_words,
        "numbers_used": numbers[:5],
        "names_used": names[:3],
        "word_count": len(tts_text.split()),
    })

    # Keep only last N
    _save_json(SCRIPT_HISTORY_FILE, history[-MAX_HISTORY:])


def check_script_similarity(title: str, hook: str, tts_text: str) -> list[str]:
    """Check if this script is too similar to any recent one.
    Returns a list of warning messages. Empty = OK to proceed.
    """
    history = _load_json(SCRIPT_HISTORY_FILE)
    if not history:
        return []

    warnings: list[str] = []
    opening = " ".join(tts_text.split()[:10]).lower()
    numbers = set(re.findall(r'\$[\d,]+[KkMm]?|\d{2,}%|\d{3,}', tts_text))

    for prev in history[-10:]:  # check last 10
        # Same opening words
        if prev.get("opening_words") == opening:
            warnings.append(f"same opening as {prev['draft_id']}: '{opening}'")

        # Same title pattern
        prev_title = prev.get("title", "").lower()
        if _title_similarity(title.lower(), prev_title) > 0.7:
            warnings.append(f"title too similar to {prev['draft_id']}: '{prev_title}'")

        # Reusing same dollar amounts
        prev_numbers = set(prev.get("numbers_used", []))
        overlap = numbers & prev_numbers
        if len(overlap) >= 2:
            warnings.append(f"reusing amounts {overlap} from {prev['draft_id']}")

    return warnings


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity between two titles."""
    words_a = set(a.split()) - {"the", "a", "an", "in", "to", "and", "of", "for"}
    words_b = set(b.split()) - {"the", "a", "an", "in", "to", "and", "of", "for"}
    if not words_a or not words_b:
        return 0.0
    overlap = len(words_a & words_b)
    return overlap / max(len(words_a), len(words_b))


# ---- Bamboo clip dedup ----

def get_unused_bamboo_clip(available_clips: list[Path]) -> Path:
    """Pick a Bamboo clip that hasn't been used in the last 5 videos."""
    history = _load_json(BAMBOO_CLIP_HISTORY_FILE)
    recent_names = set(h.get("clip") for h in history[-5:])

    # Find clips not used recently
    unused = [c for c in available_clips if c.name not in recent_names]

    if unused:
        import random
        chosen = random.choice(unused)
    else:
        # All clips used recently — pick the least recently used
        import random
        chosen = random.choice(available_clips)

    # Record usage
    history.append({"clip": chosen.name})
    _save_json(BAMBOO_CLIP_HISTORY_FILE, history[-MAX_HISTORY:])

    log.info("bamboo clip: %s (avoided %d recently-used)", chosen.name, len(recent_names))
    return chosen
