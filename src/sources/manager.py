"""Orchestrates source fetchers per niche and returns grounded snippets.

The manager is the ONLY entry point the script generator is allowed to use.
If gather() returns fewer than MIN_SNIPPETS, the caller must abort — we do
not write scripts on thin evidence.

Two-stage search:
  1. Derive 3 broad search queries from the (narrative) topic via Ollama,
     because the new LLM-generated topics are too specific for Google News
     to match verbatim (e.g. "A 42-year-old tech entrepreneur in Silicon
     Valley lost her $500K startup investment" returns 0 hits, but
     "startup failure causes" returns plenty).
  2. Run each fetcher against each query, dedupe by URL, return the union.

A short noun-phrase regex fallback runs if Ollama is unreachable, so we still
attempt source gathering even if the local LLM is down.
"""
from __future__ import annotations

import json
import re

import ollama

from src.core.config import env
from src.core.logging import get_logger
from src.core.models import Niche, SourceSnippet
from src.sources.base import SourceFetcher
from src.sources.fetchers.google_news import (
    APA_GN_FETCHER, BLS_FETCHER, CFPB_GN_FETCHER,
    GOTTMAN_GN_FETCHER, INVESTOR_GN_FETCHER, IRS_FETCHER, WIRE_FETCHER,
)
from src.sources.fetchers.semantic_scholar import SemanticScholarFetcher

log = get_logger(__name__)

MIN_SNIPPETS = 3  # below this, abort — insufficient grounding


REGISTRY: dict[Niche, list[SourceFetcher]] = {
    Niche.career: [BLS_FETCHER, WIRE_FETCHER],
    Niche.finance: [INVESTOR_GN_FETCHER, CFPB_GN_FETCHER, IRS_FETCHER, WIRE_FETCHER],
    Niche.love: [APA_GN_FETCHER, GOTTMAN_GN_FETCHER, SemanticScholarFetcher(), WIRE_FETCHER],
}


# Broad themes used by the regex fallback when Ollama is unreachable.
_NICHE_FALLBACK_THEMES: dict[Niche, list[str]] = {
    Niche.finance: ["personal finance", "consumer debt", "household budget"],
    Niche.career: ["unemployment statistics", "salary trends", "workforce"],
    Niche.love: ["relationship finances", "couples money"],
}


def _llm_derive_search_queries(topic: str, niche: Niche, n: int = 3) -> list[str]:
    """Convert a narrative topic into broad authoritative-source search queries.

    The source fetchers (CFPB, IRS, investor.gov, Reuters wires, BLS) need
    BROAD financial theme keywords — they have articles about "credit card
    debt trends" not about "Maria's $40K credit card mistake". This call
    extracts the underlying themes.
    """
    client = ollama.Client(host=env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model = env("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")

    prompt = f"""Convert this narrative story premise into {n} broad SEARCH QUERIES
that will match real news articles about the underlying financial topic.

Each query: 3-6 words, broad and impersonal. Focus on the FINANCIAL THEME or
PATTERN — never the specific person, name, dollar amount, or city.

Good queries: "credit card debt statistics", "401k early withdrawal penalty",
"home equity loan risks", "startup bankruptcy causes", "irs tax debt forgiveness".

Bad queries (too specific): "42 year old tech entrepreneur losses",
"Maria's $40K credit card", "Silicon Valley startup home mortgage".

NICHE: {niche.value}
TOPIC: {topic}

Respond with ONLY this JSON object (no prose):
{{"queries": ["query 1", "query 2", "query 3"]}}
"""

    try:
        resp = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.3, "num_ctx": 2048},
            format="json",
        )
        data = json.loads(resp["message"]["content"])
        queries = [
            q.strip() for q in data.get("queries", [])
            if isinstance(q, str) and 4 <= len(q.strip()) <= 80
        ]
        return queries[:n]
    except Exception as e:
        log.warning("llm_derive_search_queries failed: %s", e)
        return []


def _regex_fallback_queries(topic: str, niche: Niche) -> list[str]:
    """Pull money/finance noun-phrases from the topic as a fallback search.
    Used only if the LLM call fails.
    """
    keywords = re.findall(
        r"\b(debt|credit card|loan|mortgage|savings|investment|401k|ira|"
        r"tax|irs|stock|crypto|bitcoin|inflation|interest rate|bankruptcy|"
        r"foreclosure|salary|wage|income|rent|housing|insurance|pension|"
        r"retirement|bank|wire|fraud|scam|fee|subscription|paycheck|"
        r"layoff|fired|gig|side hustle|startup|business)\b",
        topic, re.I,
    )
    seeds = []
    seen = set()
    for kw in keywords:
        norm = kw.lower().strip()
        if norm and norm not in seen:
            seen.add(norm)
            seeds.append(norm)
    if not seeds:
        return _NICHE_FALLBACK_THEMES.get(niche, ["personal finance"])
    return seeds[:3]


def derive_search_queries(topic: str, niche: Niche) -> list[str]:
    """Public: derive 3 broad search queries from a narrative topic.

    Tries Ollama first, falls back to regex-based keyword extraction.
    """
    queries = _llm_derive_search_queries(topic, niche, n=3)
    if not queries:
        queries = _regex_fallback_queries(topic, niche)
        log.info("source queries (regex fallback): %s", queries)
    else:
        log.info("source queries (llm): %s", queries)
    return queries


def gather(niche: Niche, topic: str, per_source_limit: int = 3) -> list[SourceSnippet]:
    """Run niche fetchers against derived search queries; dedupe by URL."""
    queries = derive_search_queries(topic, niche)
    fetchers = REGISTRY[niche]
    seen_urls: set[str] = set()
    all_snips: list[SourceSnippet] = []
    for q in queries:
        for f in fetchers:
            try:
                snips = f.fetch(q, limit=per_source_limit)
                log.info("%s/%r → %s: %d snippets", niche.value, q[:40], f.source_id, len(snips))
                for s in snips:
                    if s.url not in seen_urls:
                        seen_urls.add(s.url)
                        all_snips.append(s)
            except Exception as e:
                log.warning("%s fetcher failed: %s", f.source_id, e)
        # Early-out once we have enough — avoids burning extra HTTP calls
        if len(all_snips) >= MIN_SNIPPETS * 2:
            break
    return all_snips


def enough_to_write(snips: list[SourceSnippet]) -> bool:
    return len(snips) >= MIN_SNIPPETS
