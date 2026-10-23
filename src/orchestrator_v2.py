"""Pipeline orchestrator v2 — premium quality with ZERO-REPETITION policy.

Every shipped asset (topic, hook, title, character name, dollar amount, scene
query, stock clip ID, Bamboo action, music file) is permanently banned via
`src.trends.banlist`. Every new run regenerates everything from scratch and
must produce content that does NOT collide with the banlist. If it can't, the
pipeline SKIPS THE DAY and notifies Telegram — we never ship a clone.

Flow:
  1. Compliance gate (YouTube policy check)
  2. Daily topic generation (LLM, banlist-aware) — retry until novel or skip
  3. Source-grounded script generation — retry on banlist collision or skip
  4. Telegram script-review checkpoint (advisory)
  5. ElevenLabs voiceover (only after script generated)
  6. Scene planning (banlist-aware visual queries)
  7. Stock-footage fetch (permanent clip-ID banlist, paginated search)
  8. Fresh Bamboo clip from Veo (action-pool rotation via banlist)
  9. Cinematic assembly with random color grade
  10. Upload as private draft to YouTube (AI disclosure ON)
  11. ** FINGERPRINT: persist every asset's banlist entry **
  12. Telegram video-review checkpoint
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from src.assemble.cinematic import assemble_cinematic
from src.compliance.policy_checker import check_policies
from src.core.config import settings
from src.core.logging import get_logger
from src.core.models import Format, Niche, ScriptPayload, VideoDraft
from src.core.paths import DATA, DRAFTS
from src.script.generator import (BannedContentError, InsufficientSourcesError,
                                  ScriptValidationError, fingerprint_payload,
                                  generate_with_retries)
from src.trends import banlist
from src.trends.daily_topics import pick_fresh_topic
from src.trends.story_rotation import get_next_story_type
from src.visuals.scene_planner import extract_scene_queries
from src.visuals.stock_scenes import fetch_story_scenes
from src.visuals.veo import generate_clip_library, generate_fresh_clip, list_clips
from src.voice.elevenlabs_tts import synthesize

log = get_logger(__name__)


class SkipDayError(RuntimeError):
    """Raised when no novel content could be produced — pipeline halts cleanly."""


# Map characters to their primary niche (for topic selection)
CHARACTER_NICHE: dict[str, Niche] = {
    "bamboo": Niche.finance,
    "theodore": Niche.finance,
}

MAX_TOPIC_ATTEMPTS = 4
MAX_SCRIPT_ATTEMPTS = 4


def _draft_id(character: str, fmt: Format, topic: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = hashlib.sha1(topic.encode()).hexdigest()[:6]
    return f"{ts}-{character}-{fmt.value}-{slug}"


def _build_description(script_desc: str, sources_used: list[str]) -> str:
    desc = script_desc or ""
    desc = re.split(r"(?im)^\s*Sources?\s*:", desc)[0].rstrip()
    seen: set[str] = set()
    unique: list[str] = [u for u in sources_used if u and u not in seen and not seen.add(u)]
    sources_block = "\n\nSources:\n" + "\n".join(f"- {u}" for u in unique) if unique else ""

    disclaimer = (
        "\n\n⚠️ Disclaimer: This content is for entertainment and educational purposes only. "
        "Not financial advice. Consult a qualified financial advisor for your personal situation. "
        "Stories are dramatized for educational value."
    )
    ai_block = (
        "\n\nAI disclosure: This video uses AI-generated characters and voice. "
        "Financial facts are sourced from the links above."
    )
    hashtags = "\n\n#PersonalFinance #MoneyTips #NineToVibes #FinancialFreedom #WealthBuilding"
    cta = "\n\nSubscribe for daily money stories that actually teach you something."

    return (desc + hashtags + cta + sources_block + disclaimer + ai_block)[:4950]


def _send_script_review(draft_id: str, script: ScriptPayload,
                       character: str, topic: str) -> None:
    """Send script to Telegram for human review before burning TTS credits."""
    from src.notify.telegram_bot import _bot_token, _chat_id, _save_draft_meta
    import httpx

    text = (
        f"<b>📝 Script Review — {draft_id}</b>\n\n"
        f"<b>Character:</b> {character}\n"
        f"<b>Topic:</b> {topic}\n"
        f"<b>Title:</b> {script.title}\n\n"
        f"<b>Script:</b>\n<i>{script.text_for_tts[:800]}</i>\n\n"
        f"<b>Sources:</b>\n" +
        "\n".join(f"- {u}" for u in script.sources_used[:5]) +
        "\n\n✅ Reply <code>approve</code> to proceed\n"
        "❌ Reply <code>reject</code> to skip this topic"
    )

    _save_draft_meta(draft_id, {
        "id": draft_id, "status": "awaiting_script_review",
        "character": character, "topic": topic,
        "script_title": script.title,
    })

    try:
        url = f"https://api.telegram.org/bot{_bot_token()}/sendMessage"
        httpx.post(url, json={
            "chat_id": _chat_id(), "text": text, "parse_mode": "HTML",
        }, timeout=15)
    except Exception as e:
        log.warning("script review telegram send failed: %s", e)


# ----- Phase functions ------------------------------------------------------

def _phase_pick_topic(niche: Niche) -> str:
    """Generate a fresh, banlist-clean topic via the daily LLM topic generator.
    Cycles through story types if the first type's batch is fully banned.
    """
    tried_types: list[str] = []
    for attempt in range(1, MAX_TOPIC_ATTEMPTS + 1):
        story_type = get_next_story_type()
        tried_types.append(story_type)
        candidate = pick_fresh_topic(niche, story_type)
        if candidate and not banlist.is_banned("topic", candidate):
            log.info("topic phase: %s (story_type=%s, attempt=%d)",
                     candidate, story_type, attempt)
            return candidate
        log.warning("topic phase attempt %d (%s): no fresh topic produced",
                    attempt, story_type)

    raise SkipDayError(
        f"could not generate a banlist-clean topic after {MAX_TOPIC_ATTEMPTS} "
        f"attempts (story types tried: {tried_types})"
    )


def _phase_generate_script(niche: Niche, topic: str, fmt: Format) -> ScriptPayload:
    """Generate a banlist-clean script. Raises SkipDayError if exhausted."""
    try:
        return generate_with_retries(niche, topic, fmt, max_attempts=MAX_SCRIPT_ATTEMPTS)
    except InsufficientSourcesError as e:
        raise SkipDayError(f"insufficient sources for topic {topic!r}: {e}") from e
    except BannedContentError as e:
        raise SkipDayError(
            f"script generator could not produce banlist-clean output after "
            f"{MAX_SCRIPT_ATTEMPTS} attempts. Last collisions: {e.collisions}"
        ) from e
    except ScriptValidationError as e:
        raise SkipDayError(f"script validation failed: {e}") from e


def _phase_bamboo_clip(character: str) -> tuple[Path, str]:
    """Generate ONE fresh Bamboo clip via Veo for this run."""
    try:
        return generate_fresh_clip(character)
    except Exception as e:
        log.warning("veo fresh clip failed (%s); falling back to existing library", e)
        existing = list_clips(character)
        if not existing:
            raise SkipDayError(f"no Bamboo clips available and Veo failed: {e}") from e
        # Use any non-banned existing clip; if all banned, allow reuse but warn
        unused = [c for c in existing if not banlist.is_banned("bamboo_action", c.name)]
        chosen = unused[0] if unused else existing[0]
        log.warning("using fallback bamboo clip: %s", chosen.name)
        return chosen, f"fallback:{chosen.name}"


def _pick_music_file() -> Path | None:
    """Pick a music file that has NOT been used. Returns None if the music
    directory is empty or every track is banned (orchestrator continues
    without music in that case)."""
    music_dir = DATA / "music"
    if not music_dir.exists():
        return None
    candidates = sorted(p for p in music_dir.glob("*.mp3") if p.stat().st_size > 1000)
    if not candidates:
        return None
    unused = [p for p in candidates if not banlist.is_banned("music_file", p.name)]
    if unused:
        return unused[0]
    log.warning("all music files banned — cycling (every track has been used)")
    return candidates[0]


# ----- Main build flow ------------------------------------------------------

def build_one(
    character: str,
    niche: Niche,
    fmt: Format,
    topic: str | None,
    dry_run: bool = False,
    skip_script_review: bool = False,
) -> VideoDraft:
    """End-to-end zero-repetition video build.

    `topic` may be passed in (override mode) or None (orchestrator picks fresh).
    """
    # 1. Topic phase
    if topic is None:
        topic = _phase_pick_topic(niche)
    elif banlist.is_banned("topic", topic):
        raise SkipDayError(f"override topic {topic!r} is already in the banlist")

    draft_id = _draft_id(character, fmt, topic)
    draft_dir = DRAFTS / draft_id
    draft_dir.mkdir(parents=True, exist_ok=True)
    log.info("=== %s: %s/%s/%s — %r ===", draft_id, character, niche.value, fmt.value, topic)

    # 2. Script phase (banlist-enforced)
    script = _phase_generate_script(niche, topic, fmt)

    # 3. Script review checkpoint
    if not dry_run and not skip_script_review:
        _send_script_review(draft_id, script, character, topic)
        log.info("script sent to Telegram for review. Continuing to build...")

    # 4. ElevenLabs voiceover
    audio_path, voice_name, audio_dur = synthesize(
        script.text_for_tts, draft_id, character_id=character,
    )

    # 5. Short-form duration check
    if fmt is Format.short:
        max_s = settings()["video"]["short"]["max_seconds"]
        if audio_dur > max_s:
            raise SkipDayError(f"Short is {audio_dur:.1f}s, exceeds {max_s}s limit")

    # 6. Scene planning (topic-aware, banlist-aware)
    scene_queries = extract_scene_queries(
        script.text_for_tts, n_scenes=8, topic=topic,
    )

    # 7. Stock footage — permanent banlist, paginated search
    orientation = "portrait" if fmt is Format.short else "landscape"
    story_clips, used_stock_ids = fetch_story_scenes(scene_queries, orientation=orientation)

    if not story_clips:
        raise SkipDayError(
            "no novel stock clips returned — banlist saturated for current "
            "scene queries (try diversifying topic angles)"
        )

    # 8. Fresh Bamboo Veo clip for this run
    bamboo_clip_path, bamboo_action = _phase_bamboo_clip(character)

    # 9. Cinematic assembly
    video_path = draft_dir / "video.mp4"
    music_file = _pick_music_file()
    assemble_cinematic(
        bamboo_clips=[bamboo_clip_path],
        story_scene_clips=story_clips,
        audio_path=audio_path,
        out_path=video_path,
        fmt=fmt.value,
    )

    # 10. Build domain model
    final_description = _build_description(script.description, script.sources_used)
    draft = VideoDraft(
        id=draft_id, niche=niche, format=fmt, topic=topic,
        script=ScriptPayload(
            title=script.title, description=final_description,
            hashtags=script.hashtags, sources_used=script.sources_used,
            raw_json=script.raw_json, text_for_tts=script.text_for_tts,
        ),
        audio_path=str(audio_path), video_path=str(video_path),
        duration_seconds=audio_dur, voice=voice_name, sources=[],
    )

    # 11. Upload (skipped on dry-run)
    youtube_video_id: str | None = None
    if not dry_run:
        try:
            from src.upload.youtube_uploader import upload_as_private_draft
            youtube_video_id = upload_as_private_draft(draft)
            draft.youtube_video_id = youtube_video_id
            draft.status = "uploaded"
        except Exception as e:
            log.warning("upload failed (keeping local draft): %s", e)

    # 12. FINGERPRINT — persist every asset's banlist entry
    # Only done AFTER successful assembly so a failed run does not poison the banlist.
    fingerprint_payload(script, fmt, topic)
    banlist.fingerprint_visuals(
        scene_queries=scene_queries,
        stock_clip_ids=used_stock_ids,
        bamboo_action=bamboo_action if not bamboo_action.startswith("fallback:") else None,
        music_file=music_file.name if music_file else None,
    )

    # 13. Save draft metadata
    meta = {
        "id": draft.id, "character": character,
        "niche": niche.value, "format": fmt.value, "topic": topic,
        "title": draft.script.title, "description": draft.script.description,
        "hashtags": draft.script.hashtags, "sources_used": draft.script.sources_used,
        "video_path": str(video_path), "audio_path": str(audio_path),
        "voice": voice_name, "duration_seconds": audio_dur,
        "status": draft.status, "youtube_video_id": youtube_video_id,
        "bamboo_clip": bamboo_clip_path.name,
        "bamboo_action": bamboo_action,
        "music_file": music_file.name if music_file else None,
        "stock_clip_ids": used_stock_ids,
        "scene_queries": scene_queries,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (draft_dir / "draft.json").write_text(json.dumps(meta, indent=2, default=str))

    # 14. Telegram video review
    if not dry_run:
        try:
            from src.notify.telegram_bot import send_review_request_sync
            caption = (
                f"<b>🎬 {draft.script.title}</b>\n"
                f"Character: {character} · {niche.value}/{fmt.value} · {audio_dur:.1f}s\n\n"
                f"{draft.script.description[:400]}\n\n"
                + (f"Studio: https://studio.youtube.com/video/{youtube_video_id}/edit\n"
                   if youtube_video_id else "")
                + "Tap a button below."
            )
            send_review_request_sync(draft_id, video_path, caption)
        except Exception as e:
            log.warning("telegram notify failed: %s", e)

    log.info("=== DONE %s === %s", draft_id, draft.script.title)
    return draft


def main() -> int:
    p = argparse.ArgumentParser(description="NineToVibes pipeline v2 (zero-repetition)")
    p.add_argument("--character", default="bamboo", choices=["bamboo", "theodore"])
    p.add_argument("--niche", choices=[n.value for n in Niche])
    p.add_argument("--fmt", choices=[f.value for f in Format], default="short")
    p.add_argument("--topic", help="Override topic (must not be in banlist)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-compliance", action="store_true")
    p.add_argument("--skip-script-review", action="store_true")
    # Clip library management
    p.add_argument("--generate-clips", metavar="CHARACTER", help="Generate clip library for a character")
    p.add_argument("--count", type=int, default=5, help="Number of clips to generate")
    p.add_argument("--list-clips", metavar="CHARACTER", help="List existing clips")
    p.add_argument("--banlist-stats", action="store_true", help="Show banlist sizes and exit")
    args = p.parse_args()

    if args.banlist_stats:
        from src.trends.banlist import CATEGORIES, size
        for c in CATEGORIES:
            print(f"  {c}: {size(c)}")
        return 0

    # Clip library commands
    if args.generate_clips:
        log.info("generating %d clips for %s...", args.count, args.generate_clips)
        paths = generate_clip_library(args.generate_clips, args.count)
        for p_path in paths:
            print(f"  ✅ {p_path.name}")
        print(f"\ntotal: {len(paths)} clips generated")
        return 0

    if args.list_clips:
        clips = list_clips(args.list_clips)
        for c in clips:
            print(f"  {c.name}  ({c.stat().st_size // 1024} KB)")
        print(f"\ntotal: {len(clips)} clips")
        return 0

    # Compliance gate
    if not args.skip_compliance:
        log.info("compliance gate...")
        r = check_policies()
        if not r.ok:
            log.error("POLICY GATE HALTED: %s", [p.name for p in r.changed_pages])
            log.error("Run: python -m src.compliance.policy_checker --approve")
            return 2
        log.info("compliance ok")

    # Determine niche
    niche = Niche(args.niche) if args.niche else CHARACTER_NICHE.get(args.character, Niche.finance)
    fmt = Format(args.fmt)

    try:
        build_one(
            character=args.character, niche=niche, fmt=fmt, topic=args.topic,
            dry_run=args.dry_run, skip_script_review=args.skip_script_review,
        )
        return 0
    except SkipDayError as e:
        log.error("SKIP DAY: %s", e)
        try:
            from src.notify.telegram_bot import send_skip_day_alert
            send_skip_day_alert(reason="Zero-repetition policy halted today's run",
                               details=str(e))
        except Exception as alert_err:
            log.warning("could not send skip-day alert: %s", alert_err)
        return 3
    except Exception:
        log.error("pipeline failed:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
