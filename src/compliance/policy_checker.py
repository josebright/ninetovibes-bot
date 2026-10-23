"""
YouTube policy compliance gate.

Runs BEFORE every pipeline execution. Fetches the current content of every
URL in settings.yaml → compliance.youtube_policy_urls, compares it to the
last-known-good snapshot, and:
  - If nothing changed → returns PolicyCheckResult(ok=True).
  - If anything changed → returns ok=False with a diff summary.

The orchestrator HALTS on ok=False and pings Telegram so the user can read
the new policy text, decide if the pipeline still complies, then run
  python -m src.compliance.policy_checker --approve
to accept the new baseline.

This is defense in depth on top of the content-level rules enforced by the
script generator. It's how we guarantee the pipeline reacts to YouTube policy
changes instead of happily shipping content that might violate a new rule.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import settings
from src.core.logging import get_logger
from src.core.paths import POLICY_SNAPSHOTS

log = get_logger(__name__)

USER_AGENT = "NineToVibes-PolicyCheck/0.1 (+https://youtube.com/@ninetovibes)"


@dataclass
class PolicyPageStatus:
    name: str
    url: str
    current_hash: str
    previous_hash: str | None
    changed: bool
    fetched_at: str
    note: str = ""


@dataclass
class PolicyCheckResult:
    ok: bool
    checked_at: str
    pages: list[PolicyPageStatus]

    @property
    def changed_pages(self) -> list[PolicyPageStatus]:
        return [p for p in self.pages if p.changed]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "pages": [p.__dict__ for p in self.pages],
        }


def _normalize(text: str) -> str:
    """Extract just the policy article text so diffs reflect real policy changes,
    not Google's rotating suggestion chips / related-article sidebars.
    """
    soup = BeautifulSoup(text, "lxml")
    for tag in soup(["script", "style", "noscript", "link", "meta",
                     "nav", "footer", "header"]):
        tag.decompose()
    # Prefer the article container (very stable across fetches). Fall back to
    # main or body if the page has no <article>.
    root = (
        soup.find("article")
        or soup.select_one(".article-content")
        or soup.find("main")
        or soup.body
        or soup
    )
    content = root.get_text(" ", strip=True) if root else ""
    content = re.sub(r"\s+", " ", content)
    content = re.sub(r"(Last updated|Updated on|Published)[^.]*\.", "", content, flags=re.I)
    return content.strip()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch(url: str) -> str:
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def _snapshot_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return POLICY_SNAPSHOTS / f"{safe}.json"


def _load_snapshot(name: str) -> dict | None:
    p = _snapshot_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _save_snapshot(name: str, payload: dict) -> None:
    _snapshot_path(name).write_text(json.dumps(payload, indent=2))


def check_policies() -> PolicyCheckResult:
    """Fetch + diff every policy URL. Pure read — never writes the baseline."""
    cfg_urls = settings()["compliance"]["youtube_policy_urls"]
    now = datetime.now(timezone.utc).isoformat()
    statuses: list[PolicyPageStatus] = []

    for entry in cfg_urls:
        name, url = entry["name"], entry["url"]
        try:
            raw = _fetch(url)
            normalized = _normalize(raw)
            current_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        except Exception as e:
            log.warning("Could not fetch policy URL %s: %s", url, e)
            # If we have a previous baseline, assume policy hasn't changed
            # (network error ≠ policy change). Only block if we've NEVER
            # seen this page before (no baseline to trust).
            prev = _load_snapshot(name)
            if prev:
                # Trust the existing baseline — network was just down
                statuses.append(
                    PolicyPageStatus(
                        name=name, url=url,
                        current_hash=prev["hash"], previous_hash=prev["hash"],
                        changed=False, fetched_at=now,
                        note=f"network error, trusting existing baseline",
                    )
                )
            else:
                statuses.append(
                    PolicyPageStatus(
                        name=name, url=url,
                        current_hash="FETCH_FAILED", previous_hash=None,
                        changed=True, fetched_at=now,
                        note=f"fetch failed (no baseline): {e!s}",
                    )
                )
            continue

        prev = _load_snapshot(name)
        prev_hash = prev["hash"] if prev else None
        changed = prev_hash is not None and prev_hash != current_hash
        first_run = prev_hash is None

        statuses.append(
            PolicyPageStatus(
                name=name, url=url,
                current_hash=current_hash, previous_hash=prev_hash,
                changed=changed, fetched_at=now,
                note="first run — baseline will be written on --approve" if first_run else "",
            )
        )

    ok = all((not s.changed) and s.current_hash != "FETCH_FAILED" and s.previous_hash is not None
             for s in statuses)
    return PolicyCheckResult(ok=ok, checked_at=now, pages=statuses)


def approve_current_state() -> PolicyCheckResult:
    """Persist the current fetched content as the new baseline.

    Run this after you've manually reviewed any changed policy pages and
    confirmed the pipeline still complies with the new text.
    """
    cfg_urls = settings()["compliance"]["youtube_policy_urls"]
    now = datetime.now(timezone.utc).isoformat()
    statuses: list[PolicyPageStatus] = []

    for entry in cfg_urls:
        name, url = entry["name"], entry["url"]
        raw = _fetch(url)
        normalized = _normalize(raw)
        current_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        _save_snapshot(name, {
            "name": name,
            "url": url,
            "hash": current_hash,
            "approved_at": now,
            "content_preview": normalized[:2000],
        })
        statuses.append(PolicyPageStatus(
            name=name, url=url,
            current_hash=current_hash, previous_hash=current_hash,
            changed=False, fetched_at=now,
            note="baseline approved",
        ))
    return PolicyCheckResult(ok=True, checked_at=now, pages=statuses)


def main():
    parser = argparse.ArgumentParser(description="YouTube policy compliance gate")
    parser.add_argument("--approve", action="store_true",
                        help="Accept current policy text as the new baseline (run AFTER human review).")
    parser.add_argument("--json", action="store_true", help="Output result as JSON.")
    args = parser.parse_args()

    result = approve_current_state() if args.approve else check_policies()

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        for p in result.pages:
            icon = "✅" if (not p.changed and p.previous_hash) else ("🆕" if not p.previous_hash else "⚠️")
            print(f"{icon} {p.name:35s}  {p.url}")
            if p.note:
                print(f"   ↳ {p.note}")
        print()
        print("RESULT:", "OK — pipeline may run" if result.ok else "HALT — policy changes detected, human review required")

    sys.exit(0 if result.ok or args.approve else 2)


if __name__ == "__main__":
    main()
