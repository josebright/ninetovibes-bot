"""Domain-filtered Google News RSS fetcher.

Why this exists: most authoritative public sites (BLS, APA, investor.gov, etc.)
either block bot traffic, require heavy JS, or have volatile DOM structure.
Google News already indexes their content and exposes a stable RSS endpoint
filtered by query and domain. We use that as a reliable pointer layer, then
display the original URL + title + summary in the script's Sources: list so
attribution points to the authoritative source, not to Google.

Each instance is configured with:
  - source_id: short slug used for caching + logs
  - citation_prefix: how the snippet is attributed in the final script
  - domains: list of "site:..." filters; results from other domains are dropped
  - license: licensing note passed through to SourceSnippet
"""
from __future__ import annotations
from urllib.parse import quote_plus, urlparse

import feedparser
from bs4 import BeautifulSoup

from src.core.logging import get_logger
from src.core.models import SourceSnippet
from src.sources.base import SourceFetcher

log = get_logger(__name__)

# Note on Google News URLs: the `link` field on each RSS entry is a signed
# redirector that we cannot resolve without a browser-like click flow. Google
# does, however, expose the source publication's homepage under `entry.source.href`.
# We use that as the citation URL — clean domain, always valid — and surface the
# article headline as the snippet title so a viewer can search for it on that site.


class GoogleNewsDomainFetcher(SourceFetcher):
    def __init__(
        self,
        source_id: str,
        citation_prefix: str,
        domains: list[str],
        license: str = "fair use, attributed",
    ) -> None:
        self.source_id = source_id
        self.citation_prefix = citation_prefix
        self.domains = domains
        self.license = license

    def _fetch(self, query: str, limit: int) -> list[SourceSnippet]:
        site_filter = " OR ".join(f"site:{d}" for d in self.domains)
        q = quote_plus(f"{query} ({site_filter})")
        feed_url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        d = feedparser.parse(feed_url)
        out: list[SourceSnippet] = []
        for e in d.entries:
            title = e.get("title", "") or ""
            link = e.get("link", "") or ""
            # Google News wraps links; the real URL is usually in the same entry
            host = urlparse(link).netloc.lower()
            # Accept if any configured domain is a suffix of the host (handles
            # www./subdomain. prefixes transparently)
            if not any(host.endswith(dom) for dom in self.domains):
                # Some entries show news.google.com/rss/articles/... — keep them
                # only if the query itself cites the domain; otherwise drop.
                if "news.google.com" not in host:
                    continue
            summary_raw = e.get("summary", "") or e.get("description", "")
            summary = BeautifulSoup(summary_raw, "lxml").get_text(" ", strip=True)
            clean_title = title.rsplit(" - ", 1)[0]
            body = summary[:700] if summary else clean_title
            # Prefer the source homepage (clean, always valid) over the opaque
            # news.google.com redirector.
            source_info = e.get("source", {}) or {}
            resolved_url = source_info.get("href") or f"https://www.{self.domains[0]}"
            out.append(SourceSnippet(
                source_id=self.source_id,
                citation_prefix=self.citation_prefix,
                url=resolved_url,
                title=clean_title,
                snippet=body,
                license=self.license,
            ))
            if len(out) >= limit:
                break
        return out


# Pre-configured instances for each niche. The citation_prefix is what viewers
# will see at the end of the video script, so it reads cleanly.

BLS_FETCHER = GoogleNewsDomainFetcher(
    source_id="bls",
    citation_prefix="U.S. Bureau of Labor Statistics",
    domains=["bls.gov"],
    license="public domain (U.S. Government)",
)

CFPB_GN_FETCHER = GoogleNewsDomainFetcher(
    source_id="cfpb_gn",
    citation_prefix="Consumer Financial Protection Bureau",
    domains=["consumerfinance.gov"],
    license="public domain (U.S. Government)",
)

INVESTOR_GN_FETCHER = GoogleNewsDomainFetcher(
    source_id="investor_gn",
    citation_prefix="U.S. Securities and Exchange Commission (Investor.gov)",
    domains=["investor.gov", "sec.gov"],
    license="public domain (U.S. Government)",
)

IRS_FETCHER = GoogleNewsDomainFetcher(
    source_id="irs",
    citation_prefix="Internal Revenue Service",
    domains=["irs.gov"],
    license="public domain (U.S. Government)",
)

APA_GN_FETCHER = GoogleNewsDomainFetcher(
    source_id="apa_gn",
    citation_prefix="American Psychological Association",
    domains=["apa.org"],
    license="fair use, attributed",
)

GOTTMAN_GN_FETCHER = GoogleNewsDomainFetcher(
    source_id="gottman_gn",
    citation_prefix="The Gottman Institute",
    domains=["gottman.com"],
    license="fair use, attributed",
)

WIRE_FETCHER = GoogleNewsDomainFetcher(
    source_id="wire",
    citation_prefix="Reuters / AP",
    domains=["reuters.com", "apnews.com"],
    license="fair use, attributed — no more than 3 consecutive words quoted",
)
