"""Scene planner — extracts coherent, character-locked visual search queries.

The #1 problem with stock footage storytelling: every search returns a DIFFERENT
person. Scene 1 shows a Black man, scene 2 shows a white woman, scene 3 shows
an elderly Asian man. The story falls apart visually.

Solution: the planner defines a CHARACTER PROFILE first, then EVERY search query
locks to that profile. We also categorize scenes into:
  - CHARACTER scenes (show the person — must be consistent)
  - OBJECT scenes (show things — documents, money, phone screens)
  - ENVIRONMENT scenes (show places — office, apartment, street)

Object and environment scenes don't need character consistency, so we use them
as "buffer" shots between character shots to hide the fact that different stock
actors appear in character shots.

Best practice structure for a 40-second story with 8 scenes:
  scene 1: CHARACTER (establish the person)
  scene 2: OBJECT (bills, credit card — no face needed)
  scene 3: CHARACTER (same search terms as scene 1 + new action)
  scene 4: ENVIRONMENT (luxury store, office, etc.)
  scene 5: OBJECT (phone screen, bank statement)
  scene 6: CHARACTER (same search terms)
  scene 7: OBJECT/ENVIRONMENT (climax visual — empty room, IRS letter)
  scene 8: OBJECT (wallet, keys on table — emotional ending, no face)

By alternating CHARACTER and OBJECT/ENVIRONMENT shots, we:
  - Reduce the number of "face" shots (fewer chances for mismatched actors)
  - Make the mismatches less noticeable (objects buffer between faces)
  - Create visual variety (not just talking heads)
"""
from __future__ import annotations

import json
import re

import ollama

from src.core.config import env
from src.core.logging import get_logger
from src.trends import banlist

log = get_logger(__name__)


def extract_scene_queries(script_text: str, n_scenes: int = 8,
                         topic: str = "") -> list[str]:
    """Extract character-locked, type-alternating visual search queries.

    `topic` is the original story premise — passed in so the planner can pivot
    visual searches off the TOPIC rather than re-deriving from script text
    alone. This keeps queries varied even if scripts share structure.

    Banned scene queries (from prior videos) are injected as a "do NOT use"
    block — the model is told to find fresh visual angles.
    """
    client = ollama.Client(host=env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model = env("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")

    banned_recent = banlist.list_recent("scene_query", limit=60)
    banned_block = "\n".join(f"- {q}" for q in banned_recent[:40]) or "(none yet)"

    prompt = f"""You are a film director planning B-roll for a finance story video.

STEP 1: Identify the main character's gender and age from this script.
STEP 2: Create exactly {n_scenes} video search queries following the ALTERNATING pattern below.

PATTERN (alternate between showing the PERSON and showing OBJECTS/PLACES):
  Scene 1: PERSON — show the character (use gender + age in query)
  Scene 2: OBJECT — show a relevant object (money, phone, documents — NO person)
  Scene 3: PERSON — show the character doing something new
  Scene 4: PLACE — show a relevant location (office, store, apartment — NO person)
  Scene 5: OBJECT — show another relevant object close-up
  Scene 6: PERSON — show the character's reaction/emotion
  Scene 7: OBJECT — show the climax object (empty wallet, IRS letter, bank statement)
  Scene 8: PLACE — show the final setting (empty room, street at night)

RULES:
- For PERSON scenes: ALWAYS include the SAME gender + age description.
  NEVER switch to "woman" or "person" or "people" mid-sequence.
- For OBJECT scenes: describe specific close-up shots of THINGS, no people visible.
- For PLACE scenes: describe empty or distant settings, no close-up faces.
- Each query: 4-7 words, specific enough for stock footage search.
- Follow the story chronologically.
- Pick visuals that fit THIS TOPIC, not generic finance imagery.

UNIQUENESS:
- Your {n_scenes} queries must NOT match or paraphrase any entry in BANNED_QUERIES below.
- Vary location, object choice, and angle relative to prior videos.

TOPIC: {topic or "(use script context)"}

BANNED_QUERIES (do NOT reuse or paraphrase any of these):
{banned_block}

SCRIPT:
{script_text}

JSON only:
{{"character": "gender + age description", "queries": ["query1", "query2", ...]}}"""

    resp = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.6, "num_ctx": 4096},
        format="json",
    )

    raw = resp["message"]["content"].strip()

    try:
        parsed = json.loads(raw)
        character = parsed.get("character", "")
        queries = parsed.get("queries", [])
        if isinstance(queries, list) and len(queries) >= 3:
            queries = [q.strip().strip('"') for q in queries if isinstance(q, str) and len(q) > 3]
            log.info("character: %r, scenes: %d", character, len(queries))
            return queries[:n_scenes]
    except (json.JSONDecodeError, AttributeError):
        pass

    try:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            queries = json.loads(match.group())
            return [q.strip() for q in queries if isinstance(q, str)][:n_scenes]
    except Exception:
        pass

    log.warning("LLM scene planning failed, using smart fallback")
    return _smart_fallback(script_text, n_scenes)


def _smart_fallback(script_text: str, n: int) -> list[str]:
    """Gender-aware fallback with alternating person/object/place pattern."""
    text_lower = script_text.lower()

    male = sum(1 for w in ["he ", "his ", "him ", " man ", " guy "] if w in text_lower)
    female = sum(1 for w in ["she ", "her ", " woman ", " girl "] if w in text_lower)
    char = "young man" if male >= female else "young woman"

    return [
        f"{char} sitting at desk looking stressed",      # PERSON
        "stack of bills and credit cards on table",       # OBJECT
        f"{char} walking through city street alone",      # PERSON
        "luxury apartment building exterior night",       # PLACE
        "phone screen showing bank app low balance",      # OBJECT
        f"{char} head in hands at kitchen table",         # PERSON
        "official tax documents IRS letter close up",     # OBJECT
        "empty apartment room with boxes moving out",     # PLACE
    ][:n]
