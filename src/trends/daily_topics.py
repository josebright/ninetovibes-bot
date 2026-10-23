"""Daily LLM-generated topics — replaces the static SEED_QUERIES list.

Every run:
  1. Pulls CURRENT trending themes from YouTube (last 7 days, top viewCount)
     and Google Trends (money-keyword filtered). See `trending_signals.py`.
     Themes are clickbait-stripped — we never use raw titles.
  2. Asks Ollama for a fresh batch of one-sentence story premises for the
     given niche + story_type, with two blocks injected:
       - WHAT'S TRENDING NOW (inspiration — write in this direction)
       - BANNED_TOPICS (do NOT reuse or paraphrase anything we've shipped)
  3. Returns the first topic that is not already in the banlist.

If no fresh topic can be produced after N attempts, returns None — the
orchestrator treats that as a skip-the-day signal and pings Telegram.

If trending signals are unavailable (no API key, network failure), we
gracefully fall back to pure LLM creativity — the banlist still applies.
"""
from __future__ import annotations

import json
import re

import ollama

from src.core.config import env
from src.core.logging import get_logger
from src.core.models import Niche
from src.trends import banlist
from src.trends.trending_signals import fetch_themes

log = get_logger(__name__)


NICHE_BRIEFS: dict[Niche, str] = {
    Niche.finance: (
        "personal-finance story angles. Real-feeling scenarios about real "
        "people making real financial decisions and what happened. Vary the "
        "person (gender, age, profession, city), the dollar amounts, and the "
        "decision. Avoid generic tips. Every premise should hint at a twist."
    ),
    Niche.career: (
        "career-decision story angles. Real people making consequential "
        "job, salary, or career-pivot decisions and what happened next."
    ),
    Niche.love: (
        "money-in-relationships story angles. Couples, financial secrets, "
        "spending dynamics, and the relationship consequences."
    ),
}

STORY_TYPE_GUIDE: dict[str, str] = {
    "cautionary": "person HAD money or stability and LOST it through one specific decision or pattern",
    "success": "person was BROKE or stuck and BUILT wealth/freedom through one specific method",
    "comparison": "TWO people, same starting point, opposite outcomes (contrast structure)",
    "system_exposed": "a hidden fee, scam, trap, or systemic pattern quietly drained someone over time",
    "journey": "a real transformation over a defined time window with a twist at the end",
}


def _looks_valid(topic: str) -> bool:
    if not isinstance(topic, str):
        return False
    t = topic.strip()
    if not (30 <= len(t) <= 220):
        return False
    if re.search(r"\b(shocking|insane|you won't believe|crazy|unbelievable|wow)\b", t, re.I):
        return False
    if t.lower().startswith(("tip:", "advice:", "how to", "ways to", "things to")):
        return False
    return True


def generate_topic_batch(
    niche: Niche, story_type: str, n: int = 10, max_attempts: int = 3,
) -> list[str]:
    """Ask Ollama for n fresh, banlist-clean topics. Returns only the ones
    that pass validation AND are not already banned. May return an empty list.
    """
    banned_recent = banlist.list_recent("topic", limit=120)
    banned_block = "\n".join(f"- {t}" for t in banned_recent[:80]) or "(none yet)"
    banned_names = banlist.list_recent("name", limit=30)
    banned_numbers = banlist.list_recent("number", limit=30)

    # Pull current trending themes (cached 6h). Empty list on graceful fallback.
    try:
        trending_themes = fetch_themes(niche)
    except Exception as e:
        log.warning("trending_signals.fetch_themes failed, continuing without: %s", e)
        trending_themes = []

    if trending_themes:
        trending_block = "\n".join(f"- {t}" for t in trending_themes[:25])
        trending_instructions = (
            "These are themes currently trending on YouTube and Google "
            "search in the last 7 days. Your premises should ALIGN with "
            "these themes when natural — pick angles that ride this current "
            "attention — but craft them in your own words, never copy a theme "
            "verbatim. The goal is reach (current relevance) + uniqueness."
        )
    else:
        trending_block = "(no trending signals available right now)"
        trending_instructions = (
            "Trending signals unavailable — rely on evergreen money-story "
            "patterns. Still keep each premise concrete and specific."
        )

    type_brief = STORY_TYPE_GUIDE.get(story_type, "")
    niche_brief = NICHE_BRIEFS.get(niche, "")

    prompt = f"""You are a YouTube content strategist generating one-sentence story premises.

NICHE: {niche.value} — {niche_brief}

STORY TYPE: {story_type} — {type_brief}

WHAT'S TRENDING NOW (current attention signals — write toward these):
{trending_block}

{trending_instructions}

RULES:
1. Each premise is ONE sentence describing a SPECIFIC person making a SPECIFIC
   decision, with at least one concrete number or detail.
2. Each premise must be UNIQUE — not a paraphrase of anything in BANNED below.
3. Where natural, ANCHOR the premise to one of the trending themes above so
   the resulting video lands on what people are searching for right now.
   Do NOT copy a trending theme verbatim — reframe it as a specific story.
4. Do NOT reuse character names from BANNED_NAMES.
5. Do NOT reuse dollar figures or percentages from BANNED_NUMBERS.
6. Vary professions, cities, ages, time periods, and outcomes across the batch.
7. No clickbait words ("shocking", "you won't believe", "insane", "crazy").
8. No "tips", "how-to", or listicle framings — story premises only.
9. Do NOT echo any of these overused dollar/object hooks: Porsche, Chipotle
   receipt, $340,000, $200K, $14, IRS letter, Lamborghini, Bentley, BMW.

BANNED_TOPICS — DO NOT REUSE OR PARAPHRASE:
{banned_block}

BANNED_NAMES:
{", ".join(banned_names) or "(none yet)"}

BANNED_NUMBERS:
{", ".join(banned_numbers) or "(none yet)"}

Generate exactly {n} fresh, distinct story premises matching the {story_type}
pattern for the {niche.value} niche, anchored to the trending themes where natural.

Respond with ONLY this JSON object — no prose:
{{"topics": ["premise 1", "premise 2", "..."]}}
"""

    client = ollama.Client(host=env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model = env("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")

    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 1.0, "num_ctx": 8192},
                format="json",
            )
            raw = resp["message"]["content"]
            data = json.loads(raw)
            raw_topics = data.get("topics", [])
            if not isinstance(raw_topics, list):
                raise ValueError("topics is not a list")
            fresh = [
                t.strip() for t in raw_topics
                if _looks_valid(t) and not banlist.is_banned("topic", t)
            ]
            if fresh:
                log.info("daily_topics: generated %d fresh / %d raw for %s/%s (attempt %d)",
                         len(fresh), len(raw_topics), niche.value, story_type, attempt)
                return fresh
            log.warning("daily_topics: attempt %d returned %d topics, all stale/invalid",
                        attempt, len(raw_topics))
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.warning("daily_topics: JSON parse failed attempt %d: %s", attempt, e)
        except Exception as e:
            log.warning("daily_topics: ollama call failed attempt %d: %s", attempt, e)

    log.error("daily_topics: exhausted %d attempts producing fresh topics for %s/%s",
              max_attempts, niche.value, story_type)
    return []


def pick_fresh_topic(niche: Niche, story_type: str) -> str | None:
    batch = generate_topic_batch(niche, story_type, n=10)
    return batch[0] if batch else None
