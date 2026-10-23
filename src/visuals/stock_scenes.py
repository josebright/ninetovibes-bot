"""Stock footage fetcher for story scenes.

Given a list of scene queries (from scene_planner.py), downloads ONE clip per
query from Pexels (with Pixabay fallback). Every clip ID we have ever used is
in the persistent banlist (`stock_clip_id`) — we walk through multiple pages
of search results until we find one that has NEVER been used on this channel.

Uses portrait (9:16) clips for Shorts, landscape for long-form.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

import httpx

from src.core.config import env
from src.core.logging import get_logger
from src.core.paths import CACHE
from src.trends import banlist

log = get_logger(__name__)

STOCK_CACHE = CACHE / "stock_scenes"
STOCK_CACHE.mkdir(parents=True, exist_ok=True)

# How many pages of search results to walk before we give up on a query.
MAX_PEXELS_PAGES = 4
MAX_PIXABAY_PAGES = 3
PER_PAGE = 15


def _cache_path(provider: str, clip_id: str | int) -> Path:
    h = hashlib.sha1(f"{provider}_{clip_id}".encode()).hexdigest()[:12]
    return STOCK_CACHE / f"scene_{provider}_{h}.mp4"


def _download(url: str, out: Path) -> None:
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as r:
        r.raise_for_status()
        with out.open("wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)


def _pexels_search_page(query: str, orientation: str, page: int) -> list[dict]:
    key = env("PEXELS_API_KEY")
    if not key:
        return []
    try:
        r = httpx.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": PER_PAGE, "page": page,
                    "orientation": orientation, "size": "medium"},
            headers={"Authorization": key},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json().get("videos", [])
    except Exception as e:
        log.warning("pexels page %d failed for %r: %s", page, query, e)
        return []


def _pexels_pick_file(video: dict) -> str | None:
    files = video.get("video_files", [])
    hd = [f for f in files if f.get("height", 0) >= 720]
    picked = hd[0] if hd else (files[0] if files else None)
    return picked["link"] if picked else None


def _pixabay_search_page(query: str, orientation: str, page: int) -> list[dict]:
    key = env("PIXABAY_API_KEY")
    if not key:
        return []
    pix_orient = "vertical" if orientation == "portrait" else "horizontal"
    try:
        r = httpx.get(
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": query, "per_page": PER_PAGE, "page": page,
                    "safesearch": "true", "orientation": pix_orient},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json().get("hits", [])
    except Exception as e:
        log.warning("pixabay page %d failed for %r: %s", page, query, e)
        return []


def _pixabay_pick_url(hit: dict) -> str | None:
    vid = hit.get("videos", {})
    return (vid.get("medium", {}).get("url") or
            vid.get("small", {}).get("url"))


def _find_novel_clip(query: str, orientation: str, used_in_run: set[str]
                     ) -> tuple[str, str, str] | None:
    """Walk Pexels then Pixabay pages until we find a clip whose ID is NOT in
    the persistent banlist AND not already used this run.

    Returns (provider, clip_id_str, download_url) or None.
    """
    # Pexels first
    for page in range(1, MAX_PEXELS_PAGES + 1):
        videos = _pexels_search_page(query, orientation, page)
        if not videos:
            break
        for v in videos:
            cid = str(v["id"])
            if cid in used_in_run:
                continue
            if banlist.is_banned("stock_clip_id", f"pexels:{cid}"):
                continue
            url = _pexels_pick_file(v)
            if url:
                return ("pexels", cid, url)

    # Pixabay fallback
    for page in range(1, MAX_PIXABAY_PAGES + 1):
        hits = _pixabay_search_page(query, orientation, page)
        if not hits:
            break
        for h in hits:
            cid = str(h["id"])
            if cid in used_in_run:
                continue
            if banlist.is_banned("stock_clip_id", f"pixabay:{cid}"):
                continue
            url = _pixabay_pick_url(h)
            if url:
                return ("pixabay", cid, url)

    return None


def fetch_story_scenes(
    queries: list[str],
    orientation: str = "portrait",
) -> tuple[list[Path], list[str]]:
    """Download ONE never-before-used clip per query.

    Returns (paths, used_clip_ids_qualified). `used_clip_ids_qualified` are
    strings of the form "pexels:12345" / "pixabay:67890" — the orchestrator
    bans them after a successful render.

    No clip is ever returned that exists in the persistent banlist. If a query
    fails to produce a novel clip, the next caller-supplied simplification of
    the query is tried.
    """
    paths: list[Path] = []
    used_ids_qualified: list[str] = []
    used_in_run: set[str] = set()

    for i, query in enumerate(queries):
        result = _find_novel_clip(query, orientation, used_in_run)

        # Try simplified query if nothing novel found
        if not result:
            simple = " ".join(query.split()[:3])
            if simple != query:
                result = _find_novel_clip(simple, orientation, used_in_run)

        if not result:
            log.warning("no novel clip for scene %d: %r (banlist may be saturating for this query)", i, query)
            continue

        provider, cid, url = result
        qualified = f"{provider}:{cid}"
        cached = _cache_path(provider, cid)
        try:
            if not (cached.exists() and cached.stat().st_size > 5000):
                _download(url, cached)
            used_in_run.add(cid)
            used_ids_qualified.append(qualified)
            paths.append(cached)
            log.info("scene %d: %r → %s/%s (%dKB)", i, query, provider, cid,
                     cached.stat().st_size // 1024)
        except Exception as e:
            log.warning("download failed for %r: %s", query, e)

    log.info("fetched %d/%d novel story scenes", len(paths), len(queries))
    return paths, used_ids_qualified
