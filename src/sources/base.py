"""Source fetcher abstractions.

Every fetcher takes a topic query and returns a list of SourceSnippet — a
short, factual, verifiable paragraph with URL + citation + license info.

The script generator is only allowed to cite facts that appear in one of
these returned snippets. Anything the LLM "remembers" from training that
isn't in the grounding is considered fabrication and will be rejected.
"""
from __future__ import annotations
import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import sources_allowlist
from src.core.logging import get_logger
from src.core.models import Niche, SourceSnippet
from src.core.paths import CACHE_SOURCES

log = get_logger(__name__)

# Some public-data sites (BLS, investor.gov, CFPB) reject UAs that don't look
# like a full browser. We send a realistic desktop Safari header set — we're
# only fetching public policy/reference pages anyone can read in a browser.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
CACHE_TTL = timedelta(hours=12)


def _cache_key(source_id: str, query: str) -> Path:
    h = hashlib.sha256(f"{source_id}|{query.lower()}".encode()).hexdigest()[:16]
    safe_q = re.sub(r"[^a-z0-9]+", "_", query.lower())[:40]
    return CACHE_SOURCES / f"{source_id}__{safe_q}__{h}.json"


def _read_cache(path: Path) -> list[SourceSnippet] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if datetime.fromisoformat(payload["cached_at"]) + CACHE_TTL < datetime.utcnow():
            return None
        return [SourceSnippet(**s) for s in payload["snippets"]]
    except Exception:
        return None


def _write_cache(path: Path, snippets: list[SourceSnippet]) -> None:
    payload = {
        "cached_at": datetime.utcnow().isoformat(),
        "snippets": [json.loads(s.model_dump_json()) for s in snippets],
    }
    path.write_text(json.dumps(payload, indent=2))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def http_get(url: str, extra_headers: dict | None = None, **kwargs) -> httpx.Response:
    headers = dict(BROWSER_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    # Allow per-call header overrides via kwargs['headers']; merge them
    if "headers" in kwargs:
        headers.update(kwargs.pop("headers"))
    with httpx.Client(timeout=20.0, follow_redirects=True, headers=headers) as c:
        r = c.get(url, **kwargs)
        r.raise_for_status()
        return r


class SourceFetcher(ABC):
    source_id: str = ""
    citation_prefix: str = ""
    license: str = ""

    @abstractmethod
    def _fetch(self, query: str, limit: int) -> list[SourceSnippet]:
        """Subclasses implement the actual remote fetch."""

    def fetch(self, query: str, limit: int = 3) -> list[SourceSnippet]:
        """Public entry with 12h disk cache."""
        path = _cache_key(self.source_id, query)
        cached = _read_cache(path)
        if cached is not None:
            log.info("cache hit: %s (%s)", self.source_id, query)
            return cached[:limit]
        try:
            snippets = self._fetch(query, limit)
        except Exception as e:
            log.warning("fetcher %s failed for %r: %s", self.source_id, query, e)
            return []
        _write_cache(path, snippets)
        return snippets[:limit]


def allowlist_for(niche: Niche) -> list[dict]:
    """Merge per-niche sources with shared sources from sources_allowlist.yaml."""
    al = sources_allowlist()
    entries = list(al.get(niche.value, []))
    entries.extend(al.get("shared", []))
    return entries
