"""Semantic Scholar — peer-reviewed research abstracts. Works without an API key.

License is per-paper; we only use the abstract (fair use under research quote)
and link to the paper for attribution. Never paraphrase more than 1-2 sentences
from an abstract in the final script.
"""
from __future__ import annotations

from src.core.config import env
from src.core.models import SourceSnippet
import time

from src.sources.base import SourceFetcher, http_get

ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search"


class SemanticScholarFetcher(SourceFetcher):
    source_id = "semantic_scholar"
    citation_prefix = "Peer-reviewed research (via Semantic Scholar)"
    license = "per-paper; abstracts OK, quote limits apply"

    def _fetch(self, query: str, limit: int) -> list[SourceSnippet]:
        headers = {}
        key = env("SEMANTIC_SCHOLAR_API_KEY")
        if key:
            headers["x-api-key"] = key
        params = {
            "query": query,
            "limit": max(limit * 2, 5),
            "fields": "title,abstract,url,year,authors,externalIds",
        }
        # Semantic Scholar's unauth tier is ~1 request per 3s shared across users.
        # If 429'd we politely back off rather than spamming retries.
        try:
            r = http_get(ENDPOINT, params=params, headers=headers)
            data = r.json()
        except Exception:
            time.sleep(5)
            try:
                r = http_get(ENDPOINT, params=params, headers=headers)
                data = r.json()
            except Exception:
                return []
        out: list[SourceSnippet] = []
        for p in data.get("data", []):
            abstract = (p.get("abstract") or "").strip()
            if len(abstract) < 80:
                continue
            url = p.get("url") or (
                f"https://doi.org/{p['externalIds']['DOI']}"
                if p.get("externalIds", {}).get("DOI") else ""
            )
            if not url:
                continue
            authors = ", ".join(a["name"] for a in p.get("authors", [])[:3]) or "authors n/a"
            out.append(SourceSnippet(
                source_id=self.source_id,
                citation_prefix=f"{self.citation_prefix} — {authors} ({p.get('year','n.d.')})",
                url=url,
                title=p.get("title", "Untitled paper"),
                snippet=abstract[:1200],
                license=self.license,
            ))
            if len(out) >= limit:
                break
        return out
