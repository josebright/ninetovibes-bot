"""Trending-topic picker.

Returns a ranked list of TrendingTopic for each niche. Primary signal is the
YouTube Data API search (if YOUTUBE_API_KEY is set); otherwise falls back to a
curated seed rotation so the pipeline still runs on day one without API keys.

Also dedupes against recently-used topics (persisted in data/cache/trends/used.json)
so we don't publish the same angle twice in a short window.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import env
from src.core.logging import get_logger
from src.core.models import Niche, TrendingTopic
from src.core.paths import CACHE_TRENDS

log = get_logger(__name__)

USED_FILE = CACHE_TRENDS / "used.json"
USED_TTL_DAYS = 21  # don't re-use a topic for 3 weeks

# Seed queries used both (a) to search YouTube for trending angles in each niche,
# and (b) as the fallback topic list when no API key is configured.
# Finance-focused STORY topics. Each one is a narrative, not a tip.
# The LLM turns these into stories about specific people/situations.
SEED_QUERIES: dict[Niche, list[str]] = {
    Niche.career: [
        "someone who turned down a $95K salary for startup equity that ended up worthless",
        "a person who quit their 9 to 5 and went broke in 3 months",
        "someone who got fired for negotiating their salary wrong",
        "a career switch from teacher to tech that went horribly wrong",
        "the real cost of staying at a job you hate for 10 years",
    ],
    Niche.finance: [
        # CAUTIONARY TALES (person had money, lost it)
        "a man who made $200K a year and went completely broke in 6 months",
        "a 22 year old man who inherited $800K and had nothing left by 23",
        "a man who faked being rich for 3 years until the IRS showed up at his door",
        "a woman who followed every money guru advice for a year and went $40K deeper into debt",
        "a man who maxed out 11 credit cards living a fake luxury lifestyle on Instagram",
        "a woman who made $4 million on crypto then lost it all with one phone call",

        # SUCCESS STORIES (person was broke, built wealth)
        "a woman making $35K who became debt free in 18 months using one simple method",
        "a college student who built a $50K portfolio investing just $5 a day",
        "an uber driver who retired at 40 by doing one thing differently with his money",
        "a woman who made $60K and saved more than her coworker making $150K",
        "a man who turned his $3200 tax refund into a rental property in 2 years",

        # COMPARISON STORIES (two people, same start, different outcomes)
        "two best friends started the same job at $55K and one retired at 45 while the other is still broke",
        "a couple where the husband saved everything and the wife spent everything and what happened",
        "two brothers inherited $200K each and made completely opposite decisions with it",

        # SYSTEM-EXPOSED STORIES (hidden fees, scams, traps)
        "the $7 subscription that quietly drained $14000 from a mans checking account over 5 years",
        "a man who paid $40K in unnecessary bank fees over his lifetime without knowing",
        "how a womans bank charged her $3500 in overdraft fees in a single year",
        "the hidden credit card trick that costs the average person $2300 a year",

        # JOURNEY STORIES (real transformation over time)
        "a couple who paid off $120K in debt in 18 months but their marriage didnt survive it",
        "a man who lived on $30K a year in San Francisco while secretly being a millionaire",
        "a woman who tracked every dollar for 365 days and what she discovered shocked her",
    ],
    Niche.love: [
        "a couple that fought about money every day until one conversation changed everything",
        "how financial secrets destroyed a 10 year marriage",
    ],
}


def _load_used() -> dict[str, str]:
    if not USED_FILE.exists():
        return {}
    try:
        return json.loads(USED_FILE.read_text())
    except Exception:
        return {}


def _save_used(used: dict[str, str]) -> None:
    USED_FILE.write_text(json.dumps(used, indent=2))


def _prune_used(used: dict[str, str]) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=USED_TTL_DAYS)
    kept = {}
    for topic, iso in used.items():
        try:
            if datetime.fromisoformat(iso) >= cutoff:
                kept[topic] = iso
        except ValueError:
            continue
    return kept


def mark_used(topic: str) -> None:
    used = _prune_used(_load_used())
    used[topic] = datetime.now(timezone.utc).isoformat()
    _save_used(used)


def _canonicalize(topic: str) -> str:
    return re.sub(r"\s+", " ", topic.lower()).strip()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _youtube_search(q: str, api_key: str, max_results: int = 5) -> list[dict]:
    """YouTube Data API v3 search — returns top videos matching q ordered by view count,
    published in the last 30 days. Each call costs ~100 units of the 10,000/day quota.
    """
    published_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "part": "snippet",
        "q": q,
        "type": "video",
        "order": "viewCount",
        "maxResults": max_results,
        "publishedAfter": published_after,
        "regionCode": "US",
        "relevanceLanguage": "en",
        "key": api_key,
    }
    with httpx.Client(timeout=15.0) as c:
        r = c.get("https://www.googleapis.com/youtube/v3/search", params=params)
        r.raise_for_status()
        return r.json().get("items", [])


def _derive_topic_from_title(title: str, seed: str) -> str:
    """Clean a YouTube title into a neutral topic string we can re-script ourselves.
    We NEVER copy video titles verbatim — that's on the copyright line.
    """
    title = re.sub(r"\s*[|\-:]\s*.*$", "", title)       # strip trailing ' - Channel Name'
    title = re.sub(r"[#*\[\]()\"`]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    # If the title is super clickbaity, fall back to the seed so we don't inherit tone
    if re.search(r"\b(shocking|insane|you won't believe|crazy)\b", title, re.I):
        return seed
    return title[:80]


def pick_topics(niche: Niche, n: int = 1, seed_range: tuple[int, int] | None = None) -> list[TrendingTopic]:
    """Return up to n trending topics for this niche, filtered against recent usage.

    seed_range: optional (start, end) to select topics from a specific story type
    (e.g. cautionary tales, success stories). Prevents same story type daily.
    """
    used = _prune_used(_load_used())
    _save_used(used)
    candidates: list[TrendingTopic] = []

    # Get seeds, optionally filtered by story type range
    all_seeds = SEED_QUERIES[niche]
    if seed_range:
        start, end = seed_range
        seeds = all_seeds[start:end]
        log.info("using seed range [%d:%d] (%d topics)", start, end, len(seeds))
    else:
        seeds = all_seeds

    api_key = env("YOUTUBE_API_KEY")
    if api_key:
        for seed in seeds:
            try:
                items = _youtube_search(seed, api_key, max_results=3)
            except Exception as e:
                log.warning("YT search failed for %r: %s", seed, e)
                continue
            for item in items:
                title = item["snippet"]["title"]
                topic = _derive_topic_from_title(title, seed)
                if _canonicalize(topic) in used:
                    continue
                candidates.append(TrendingTopic(
                    niche=niche, query=topic,
                    rationale=f"YT top result for seed '{seed}'", score=1.0,
                ))
                if len(candidates) >= n * 3:
                    break
            if len(candidates) >= n * 3:
                break

    # Fallback or top-up with the seed list itself
    if len(candidates) < n:
        for seed in seeds:
            if _canonicalize(seed) in used:
                continue
            candidates.append(TrendingTopic(
                niche=niche, query=seed,
                rationale="curated seed (story type rotation)",
                score=0.5,
            ))
            if len(candidates) >= n * 3:
                break

    # Dedupe by canonical form
    seen: set[str] = set()
    unique: list[TrendingTopic] = []
    for c in candidates:
        k = _canonicalize(c.query)
        if k in seen:
            continue
        seen.add(k)
        unique.append(c)

    return unique[:n]
