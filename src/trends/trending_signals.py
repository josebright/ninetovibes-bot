"""Fetch CURRENT trending signals from YouTube + Google Trends.

This module returns "themes" (clean keyword phrases) that are hot right now —
NOT verbatim video titles. The signals feed into `daily_topics.py` so the LLM
can write fresh, banlist-clean story premises that align with what viewers
are currently searching for.

KEY DESIGN CHOICE — why we extract themes instead of using titles directly:

  Pre-2026-05-11, the pipeline took YouTube top-result titles verbatim as
  topics. That's how a drama-channel title "On 1st Day Out Of Prison, She
  Saved Dying Man With Blood!" landed in the finance pipeline and produced
  garbage. YouTube's view-count sort doesn't care about niche relevance,
  and clickbait phrases poison downstream generation.

  Now we strip titles to their underlying keywords/themes and let the LLM
  craft the actual premise around them. This preserves discoverability
  (we ride current attention) while keeping the writer in control of tone.

Sources:
  1. YouTube Data API v3 — top finance videos in last 7 days by viewCount
     (uses YOUTUBE_API_KEY, ~400 quota units per refresh out of 10000/day)
  2. Google Trends daily RSS — rising US queries, money-keyword filtered
     (no API key required)
  3. Cached locally for 6 hours to conserve YouTube quota and rate limit.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from src.core.config import env
from src.core.logging import get_logger
from src.core.models import Niche
from src.core.paths import CACHE_TRENDS

log = get_logger(__name__)

SIGNALS_CACHE = CACHE_TRENDS / "trending_signals.json"
CACHE_TTL_HOURS = 6

# Search queries used to find trending videos in each niche. These are SEED
# queries that fetch real trending titles — we then extract themes from the
# titles, never copy them.
NICHE_SEED_QUERIES: dict[Niche, list[str]] = {
    Niche.finance: [
        "personal finance story",
        "money mistake",
        "debt payoff journey",
        "broke to rich",
        "tax bill nightmare",
        "credit card debt story",
        "millionaire next door",
        "frugal living story",
    ],
    Niche.career: [
        "salary negotiation story",
        "got fired story",
        "career change journey",
        "side hustle success",
    ],
    Niche.love: [
        "money problems relationship",
        "couples finances story",
        "financial infidelity",
    ],
}

# Clickbait phrases to scrub from titles before extracting themes.
CLICKBAIT_PATTERNS = [
    r"\b(shocking|insane|crazy|unbelievable|wow|hilarious|amazing|epic)\b",
    r"\byou won't believe\b",
    r"\bwait until you see\b",
    r"\bi can't believe\b",
    r"\bMUST WATCH\b",
    r"\bGONE WRONG\b",
    r"\bGONE VIRAL\b",
    r"\bPART \d+\b",
    r"[!?]{2,}",
    r"\.{3,}",
]

# Keywords that mark a Google Trends entry as money-relevant.
MONEY_KEYWORD_RE = re.compile(
    r"\b(money|debt|salary|tax|irs|stock|crypto|inflation|loan|mortgage|"
    r"rent|invest|savings|bank|fee|broke|rich|millionaire|401k|ira|"
    r"housing|recession|layoff|fired|raise|wage|income|wealth|cost|price|"
    r"credit|earnings|financial|frugal|budget|spending|afford|expense|"
    r"paycheck|payoff|paid off|net worth|retire|retirement|fortune|"
    r"finance|finances)\b",
    re.I,
)


# ---- Title cleaning --------------------------------------------------------

def _strip_clickbait(title: str) -> str:
    """Convert a clickbait title into a clean theme phrase.

    Removes channel-name suffixes ("...| ChannelName"), emojis, punctuation
    spam, and clickbait words. What remains is the underlying topic phrase.
    """
    t = title
    # Drop trailing " | Channel" / " - Channel" / " — Channel" once we hit
    # a capital-letter run that looks like a channel handle
    t = re.split(r"\s*[|\-—:]\s*(?=[A-Z@])", t, 1)[0]
    # Strip emoji and decorative chars but keep $%
    t = re.sub(r"[^\w\s$%]", " ", t)
    for pat in CLICKBAIT_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---- Cache -----------------------------------------------------------------

def _load_cache() -> dict | None:
    if not SIGNALS_CACHE.exists():
        return None
    try:
        data = json.loads(SIGNALS_CACHE.read_text())
        ts = datetime.fromisoformat(data.get("cached_at", ""))
        if datetime.now(timezone.utc) - ts < timedelta(hours=CACHE_TTL_HOURS):
            return data
    except Exception:
        return None
    return None


def _save_cache(niche: Niche, themes: list[str], sources: dict[str, int]) -> None:
    SIGNALS_CACHE.write_text(json.dumps({
        "niche": niche.value,
        "themes": themes,
        "sources": sources,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


# ---- YouTube Data API ------------------------------------------------------

def _youtube_top_titles(niche: Niche, per_query: int = 6) -> list[str]:
    """Search YouTube for top-viewed videos in the niche from the last 7 days."""
    key = env("YOUTUBE_API_KEY")
    if not key:
        log.info("trending_signals: YOUTUBE_API_KEY not set, skipping YouTube")
        return []

    seeds = NICHE_SEED_QUERIES.get(niche, [])
    published_after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    titles: list[str] = []
    # Cap to 4 searches per refresh — 4 × 100 = 400 quota units, plenty of headroom
    for q in seeds[:4]:
        try:
            r = httpx.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet", "q": q, "type": "video",
                    "order": "viewCount", "maxResults": per_query,
                    "publishedAfter": published_after,
                    "regionCode": "US", "relevanceLanguage": "en",
                    "key": key,
                },
                timeout=15,
            )
            if r.status_code != 200:
                log.warning("youtube search %r returned %d: %s",
                            q, r.status_code, r.text[:120])
                continue
            for item in r.json().get("items", []):
                title = item.get("snippet", {}).get("title", "").strip()
                if title:
                    titles.append(title)
        except Exception as e:
            log.warning("youtube search %r failed: %s", q, e)
    return titles


# ---- Google Trends ---------------------------------------------------------

def _google_trends_money_themes() -> list[str]:
    """Pull US daily trends from the public Google Trends endpoint.
    No API key required. Filters to money-relevant queries only.
    """
    try:
        r = httpx.get(
            "https://trends.google.com/trends/api/dailytrends",
            params={"hl": "en-US", "tz": "-300", "geo": "US"},
            timeout=10,
        )
        if r.status_code != 200:
            log.info("google trends returned %d", r.status_code)
            return []
        # Google prepends a JSON-hijack guard like ")]}',\n" — strip it
        body = r.text
        if body.startswith(")]}'"):
            body = body.split("\n", 1)[1] if "\n" in body else body[5:]
        data = json.loads(body)
        themes: list[str] = []
        for day in data.get("default", {}).get("trendingSearchesDays", []):
            for trend in day.get("trendingSearches", []):
                title = trend.get("title", {}).get("query", "")
                if title and MONEY_KEYWORD_RE.search(title):
                    themes.append(title)
                # Also harvest related queries
                for rq in trend.get("relatedQueries", []):
                    q = rq.get("query", "")
                    if q and MONEY_KEYWORD_RE.search(q):
                        themes.append(q)
        return themes[:25]
    except Exception as e:
        log.info("google trends fetch failed (continuing without it): %s", e)
        return []


# ---- Public API ------------------------------------------------------------

def fetch_themes(niche: Niche, force_refresh: bool = False) -> list[str]:
    """Return a deduplicated list of clickbait-stripped trending themes
    relevant to this niche. Cached for 6 hours.

    Graceful degradation: if no API keys are available or all sources fail,
    returns an empty list — `daily_topics.py` will then fall back to pure
    LLM creativity (still banlist-enforced).
    """
    if not force_refresh:
        cached = _load_cache()
        if cached and cached.get("niche") == niche.value:
            themes = cached.get("themes", [])
            log.info("trending_signals: cache hit (%d themes)", len(themes))
            return themes

    raw_titles = _youtube_top_titles(niche)
    yt_count = len(raw_titles)

    gt_count = 0
    if niche is Niche.finance:
        gt_themes = _google_trends_money_themes()
        gt_count = len(gt_themes)
        raw_titles.extend(gt_themes)

    # Clean + dedupe + relevance-filter
    themes: list[str] = []
    seen: set[str] = set()
    for raw in raw_titles:
        clean = _strip_clickbait(raw)
        key = clean.lower()
        if not (8 <= len(clean) <= 120) or key in seen:
            continue
        # Must contain at least one money-related keyword — YouTube's viewCount
        # sort routinely surfaces irrelevant viral clips even on finance seeds.
        if not MONEY_KEYWORD_RE.search(clean):
            continue
        # Must be predominantly English (loose check: > 70% ASCII letters)
        letters = [c for c in clean if c.isalpha()]
        if letters:
            ascii_ratio = sum(1 for c in letters if ord(c) < 128) / len(letters)
            if ascii_ratio < 0.85:
                continue
        seen.add(key)
        themes.append(clean)

    log.info("trending_signals: %d themes (youtube=%d, google_trends=%d) for %s",
             len(themes), yt_count, gt_count, niche.value)

    _save_cache(niche, themes, {"youtube": yt_count, "google_trends": gt_count})
    return themes
