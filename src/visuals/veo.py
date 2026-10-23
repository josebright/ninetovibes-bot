"""Google Veo video generation for character clips.

Three modes:
  1. `generate_fresh_clip(character)` — generates ONE new clip per pipeline run,
     picking an action from the pool that is NOT in the banlist. Result is
     written with a timestamped filename so it never collides with prior clips.
     This is what the daily orchestrator uses.
  2. `generate_clip_library(character, n)` — batch-generates n clips with
     different actions from the character's action_pool. Used once to build
     an initial library or as backup.
  3. `get_clips_for_video(character, n)` — picks n clips from the existing
     library. Retained for backwards compatibility / Theodore fallback only.

Clips are stored in data/clips/<character_id>/ with timestamped filenames.

Uses Veo 3.1 Lite by default ($0.05/sec = $0.40 per 8s clip).
"""
from __future__ import annotations
import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from google import genai
from google.genai import types

from src.core.config import env
from src.core.logging import get_logger
from src.core.paths import ROOT, DATA
from src.trends import banlist

log = get_logger(__name__)

CLIPS_DIR = DATA / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

# Veo 3.1 Lite is the cheapest ($0.05/sec = $0.40 per 8s clip).
# Falls back to veo-2.0 if 3.1 isn't available.
VEO_MODEL = "veo-3.1-lite-generate-preview"


def _load_character(character_id: str) -> dict[str, Any]:
    path = ROOT / "config" / "characters" / f"{character_id}.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _clip_dir(character_id: str) -> Path:
    d = CLIPS_DIR / character_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client() -> genai.Client:
    return genai.Client(api_key=env("GEMINI_API_KEY", required=True))


def generate_single_clip(
    character_id: str,
    action: str,
    output_path: Path | None = None,
) -> Path:
    """Generate one 8-second 9:16 clip via Veo and save to disk."""
    char = _load_character(character_id)
    prompt = char["base_prompt"].replace("[ACTION]", action)

    clip_dir = _clip_dir(character_id)
    if output_path is None:
        slug = hashlib.sha1(action.encode()).hexdigest()[:8]
        output_path = clip_dir / f"{character_id}_{slug}.mp4"

    if output_path.exists() and output_path.stat().st_size > 1000:
        log.info("clip already exists: %s", output_path.name)
        return output_path

    log.info("generating veo clip: %s → %s", character_id, output_path.name)
    client = _client()

    operation = client.models.generate_videos(
        model=VEO_MODEL,
        prompt=prompt,
        config=types.GenerateVideosConfig(
            aspect_ratio="9:16",
            number_of_videos=1,
        ),
    )

    # Poll until complete
    max_wait = 300  # 5 minutes max
    waited = 0
    while not operation.done:
        time.sleep(10)
        waited += 10
        operation = client.operations.get(operation)
        if waited >= max_wait:
            raise TimeoutError(f"Veo generation timed out after {max_wait}s")

    if not operation.response or not operation.response.generated_videos:
        raise RuntimeError(f"Veo returned no video: {operation}")

    video = operation.response.generated_videos[0]
    # Download the video bytes and write to disk
    video_bytes = client.files.download(file=video.video)
    with open(output_path, "wb") as f:
        f.write(video_bytes)

    log.info("veo clip saved: %s (%d bytes)", output_path.name, output_path.stat().st_size)
    return output_path


def _pick_fresh_action(character_id: str) -> str:
    """Pick an action from the pool that has not been used recently. If every
    action in the pool is banned (we've cycled through), fall back to a random
    one — Veo's generative output will still differ visually between runs."""
    char = _load_character(character_id)
    pool = list(char["action_pool"])
    if not pool:
        raise RuntimeError(f"no action_pool defined for {character_id}")

    unused = [a for a in pool if not banlist.is_banned("bamboo_action", a)]
    if unused:
        random.shuffle(unused)
        chosen = unused[0]
        log.info("bamboo action: fresh (%d/%d unused remaining)",
                 len(unused), len(pool))
    else:
        # Entire pool has been used — reset by cycling, but still pick randomly
        log.warning("bamboo action pool fully banned — cycling (every action used)")
        chosen = random.choice(pool)
    return chosen


def generate_fresh_clip(character_id: str) -> tuple[Path, str]:
    """Generate ONE brand-new Veo clip for this pipeline run.

    - Picks an action that is not in the banlist (so we cycle through the pool
      before any action repeats).
    - Writes to data/clips/<character>/<character>_<utc-ts>_<actionhash>.mp4
      with a timestamped filename, so we never overwrite previous clips and
      the file path itself is unique per run.
    - Returns (path, action_text) — the orchestrator should ban the action
      after the video has assembled successfully.

    Cost: ~$0.40 per 8s clip on Veo 3.1 Lite.
    """
    action = _pick_fresh_action(character_id)
    clip_dir = _clip_dir(character_id)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = hashlib.sha1(action.encode()).hexdigest()[:8]
    out = clip_dir / f"{character_id}_{ts}_{slug}.mp4"

    log.info("fresh bamboo clip: %s", out.name)
    generate_single_clip(character_id, action, output_path=out)
    return out, action


def generate_clip_library(character_id: str, n: int = 30) -> list[Path]:
    """Generate n clips using random actions from the character's action_pool.

    Skips actions that already have a clip on disk (idempotent).
    Cost: n × $0.40 for Veo 3.1 Lite.
    """
    char = _load_character(character_id)
    actions = char["action_pool"]
    if n > len(actions):
        # Repeat with slight variations
        actions = actions * ((n // len(actions)) + 1)
    selected = actions[:n]

    paths: list[Path] = []
    for i, action in enumerate(selected):
        log.info("library clip %d/%d for %s", i + 1, n, character_id)
        retries = 0
        while retries < 5:
            try:
                p = generate_single_clip(character_id, action)
                paths.append(p)
                break
            except Exception as e:
                err = str(e)
                if "RESOURCE_EXHAUSTED" in err or "429" in err:
                    wait = 30 * (retries + 1)  # 30s, 60s, 90s, 120s, 150s
                    log.info("rate limited, waiting %ds before retry...", wait)
                    time.sleep(wait)
                    retries += 1
                else:
                    log.warning("clip %d failed: %s", i + 1, e)
                    break
        # Delay between successful generations to stay under rate limit
        if i < n - 1:
            time.sleep(15)

    log.info("library complete: %d/%d clips for %s", len(paths), n, character_id)
    return paths


def list_clips(character_id: str) -> list[Path]:
    """List all existing clips for a character, sorted by name."""
    d = _clip_dir(character_id)
    clips = sorted(d.glob("*.mp4"))
    return [c for c in clips if c.stat().st_size > 1000]


def get_clips_for_video(character_id: str, n: int = 6) -> list[Path]:
    """Pick n clips from the library for a video.

    Uses random selection so consecutive videos don't look identical.
    If library has fewer than n clips, generates on demand.
    """
    existing = list_clips(character_id)

    if len(existing) < n:
        log.warning("only %d clips for %s, need %d — generating more", len(existing), character_id, n)
        char = _load_character(character_id)
        pool = char["action_pool"]
        needed = n - len(existing)
        for action in random.sample(pool, min(needed, len(pool))):
            try:
                p = generate_single_clip(character_id, action)
                existing.append(p)
            except Exception as e:
                log.warning("on-demand clip failed: %s", e)

    if not existing:
        raise RuntimeError(f"no clips available for {character_id}")

    # Pick n with replacement if library is small
    if len(existing) >= n:
        return random.sample(existing, n)
    return [existing[i % len(existing)] for i in range(n)]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--character", default="bamboo", help="Character ID")
    p.add_argument("--count", type=int, default=5, help="Number of clips to generate")
    p.add_argument("--list", action="store_true", help="List existing clips")
    args = p.parse_args()

    if args.list:
        for c in list_clips(args.character):
            print(f"  {c.name}  ({c.stat().st_size // 1024} KB)")
        print(f"total: {len(list_clips(args.character))} clips")
    else:
        generate_clip_library(args.character, args.count)
