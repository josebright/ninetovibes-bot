"""Telegram review + approval bot.

Two modes:
  - As a library: `send_review_request(draft)` posts a Telegram message with
    the video, metadata, and inline buttons (Publish / Reject / Reschedule).
  - As a long-running bot: `python -m src.notify.telegram_bot` polls for
    button presses and reacts to them.

Button semantics:
  - Publish:     flips the private draft to public via youtube_uploader.publish()
  - Reject:      deletes the private video from the channel
  - Reschedule:  prompts the user to reply with an ISO datetime; schedules the
                 publish via youtube_uploader then exits.

State: one JSON file per draft in data/drafts/<id>/draft.json holds
{status, youtube_video_id, ...}. The bot reads+writes these.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from src.core.config import env
from src.core.logging import get_logger
from src.core.paths import DRAFTS

log = get_logger(__name__)


def _bot_token() -> str:
    return env("TELEGRAM_BOT_TOKEN", required=True)


def _chat_id() -> str:
    return env("TELEGRAM_CHAT_ID", required=True)


def _draft_meta_path(draft_id: str) -> Path:
    return DRAFTS / draft_id / "draft.json"


def _load_draft_meta(draft_id: str) -> dict | None:
    p = _draft_meta_path(draft_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _save_draft_meta(draft_id: str, meta: dict) -> None:
    p = _draft_meta_path(draft_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2, default=str))


# ---- Outbound: send review request ---------------------------------------

def send_review_request_sync(draft_id: str, video_path: Path, caption: str) -> None:
    """Blocking helper used by the orchestrator right after a draft finishes."""
    asyncio.run(_send_review_request(draft_id, video_path, caption))


async def _send_review_request(draft_id: str, video_path: Path, caption: str) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data=f"pub:{draft_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rej:{draft_id}"),
    ], [
        InlineKeyboardButton("🕒 Reschedule", callback_data=f"sched:{draft_id}"),
        InlineKeyboardButton("♻️ Regenerate", callback_data=f"regen:{draft_id}"),
    ]])

    app = Application.builder().token(_bot_token()).build()
    async with app:
        # Telegram caps video uploads at 50MB for standard bots; above that
        # send a text summary + a YouTube Studio link.
        size_mb = video_path.stat().st_size / 1024 / 1024
        if size_mb <= 48:
            with video_path.open("rb") as f:
                await app.bot.send_video(
                    chat_id=_chat_id(), video=f, caption=caption[:1024],
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                )
        else:
            await app.bot.send_message(
                chat_id=_chat_id(), text=caption,
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )


# ---- Inbound: button handlers --------------------------------------------

async def _on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "NineToVibes reviewer bot online. I'll ping you when a video is ready."
    )


async def _on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    try:
        action, draft_id = data.split(":", 1)
    except ValueError:
        await q.edit_message_caption(caption="Unknown action.")
        return

    meta = _load_draft_meta(draft_id)
    if not meta:
        await q.edit_message_caption(caption=f"Draft {draft_id} not found (expired?).")
        return

    if action == "pub":
        await _handle_publish(q, draft_id, meta)
    elif action == "rej":
        await _handle_reject(q, draft_id, meta)
    elif action == "sched":
        await q.edit_message_caption(
            caption=(q.message.caption or "") +
                    "\n\n🕒 Reply with a UTC datetime like <code>2026-04-15T19:00:00Z</code>.",
            parse_mode=ParseMode.HTML,
        )
        ctx.user_data["awaiting_schedule"] = draft_id
    elif action == "regen":
        await q.edit_message_caption(
            caption=(q.message.caption or "") +
                    "\n\n♻️ Marked for regeneration. The next pipeline run will replace it.",
        )
        meta["status"] = "regenerate"
        _save_draft_meta(draft_id, meta)


async def _handle_publish(q, draft_id: str, meta: dict) -> None:
    from src.upload import youtube_uploader
    vid = meta.get("youtube_video_id")
    if not vid:
        await q.edit_message_caption(caption=f"❌ No YouTube ID for {draft_id}.")
        return
    try:
        youtube_uploader.publish(vid)
        meta["status"] = "published"
        _save_draft_meta(draft_id, meta)
        await q.edit_message_caption(
            caption=f"✅ Published: https://youtu.be/{vid}",
        )
    except Exception as e:
        log.exception("publish failed")
        await q.edit_message_caption(caption=f"❌ Publish failed: {e!s}")


async def _handle_reject(q, draft_id: str, meta: dict) -> None:
    from src.upload.youtube_uploader import _client  # noqa
    vid = meta.get("youtube_video_id")
    try:
        if vid:
            _client().videos().delete(id=vid).execute()
        meta["status"] = "rejected"
        _save_draft_meta(draft_id, meta)
        await q.edit_message_caption(caption=f"🗑 Rejected & deleted from YouTube.")
    except Exception as e:
        log.exception("reject failed")
        await q.edit_message_caption(caption=f"❌ Reject failed: {e!s}")


def _find_latest_draft() -> tuple[str, dict] | None:
    """Find the most recent draft with a YouTube video ID."""
    import os
    drafts_dir = DRAFTS
    if not drafts_dir.exists():
        return None
    dirs = sorted([d for d in drafts_dir.iterdir() if d.is_dir()], reverse=True)
    for d in dirs:
        meta_path = d / "draft.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("youtube_video_id"):
                    return d.name, meta
            except Exception:
                continue
    return None


async def _on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text commands: publish, reject, regenerate.

    Works with the latest draft so the user doesn't need to know draft IDs.
    """
    raw = update.message.text.strip()
    raw_lower = raw.lower()

    # If user types without / prefix, remind them to use commands
    if not raw.startswith("/"):
        known = ("publish", "reject", "regenerate", "regen", "status", "delete", "approve")
        if raw_lower in known:
            await update.message.reply_text(
                f"Use <b>/{raw_lower}</b> instead.\n\n"
                "Commands:\n"
                "• /publish — make the latest video public\n"
                "• /reject — delete from YouTube & regenerate\n"
                "• /regenerate — keep video but create a new one\n"
                "• /status — show latest draft info",
                parse_mode="HTML",
            )
            return
        # Not a known command without / — ignore or show help
        return

    # Strip / and bot username suffix (@ninetovibes_reviewer_bot)
    text = raw_lower.lstrip("/").split("@")[0].strip()

    # Check for scheduling response first
    draft_id = ctx.user_data.pop("awaiting_schedule", None)
    if draft_id:
        meta = _load_draft_meta(draft_id)
        if not meta or not meta.get("youtube_video_id"):
            await update.message.reply_text("Can't reschedule — draft missing YouTube id.")
            return
        try:
            from src.upload.youtube_uploader import _client
            body = {
                "id": meta["youtube_video_id"],
                "status": {"privacyStatus": "private", "publishAt": text,
                           "containsSyntheticMedia": True, "selfDeclaredMadeForKids": False},
            }
            _client().videos().update(part="status", body=body).execute()
            meta["status"] = "scheduled"
            _save_draft_meta(draft_id, meta)
            await update.message.reply_text(f"🕒 Scheduled for {text}")
        except Exception as e:
            await update.message.reply_text(f"❌ Schedule failed: {e!s}")
        return

    # Handle publish / reject / regenerate commands
    if text in ("publish", "/publish", "approve", "/approve"):
        result = _find_latest_draft()
        if not result:
            await update.message.reply_text("No draft found to publish.")
            return
        draft_id, meta = result
        vid = meta.get("youtube_video_id")
        try:
            from src.upload import youtube_uploader
            youtube_uploader.publish(vid)
            meta["status"] = "published"
            _save_draft_meta(draft_id, meta)
            await update.message.reply_text(
                f"✅ Published!\nhttps://youtu.be/{vid}\n\nTitle: {meta.get('title','')}"
            )
        except Exception as e:
            log.exception("publish failed")
            await update.message.reply_text(f"❌ Publish failed: {e!s}")

    elif text in ("reject", "/reject", "delete", "/delete"):
        result = _find_latest_draft()
        if not result:
            await update.message.reply_text("No draft found to reject.")
            return
        draft_id, meta = result
        vid = meta.get("youtube_video_id")
        try:
            if vid:
                from src.upload.youtube_uploader import _client
                _client().videos().delete(id=vid).execute()
            meta["status"] = "rejected"
            _save_draft_meta(draft_id, meta)
            await update.message.reply_text(
                f"🗑 Rejected & deleted from YouTube.\n\n"
                f"Title: {meta.get('title','')}\n"
                f"The next pipeline run will generate a new video."
            )
        except Exception as e:
            log.exception("reject failed")
            await update.message.reply_text(f"❌ Reject failed: {e!s}")

    elif text in ("regenerate", "/regenerate", "regen", "/regen"):
        result = _find_latest_draft()
        if not result:
            await update.message.reply_text("No draft found.")
            return
        draft_id, meta = result
        meta["status"] = "regenerate"
        _save_draft_meta(draft_id, meta)
        await update.message.reply_text(
            f"♻️ Marked for regeneration.\n"
            f"The next pipeline run will create a new video with a different topic."
        )

    elif text in ("status", "/status"):
        result = _find_latest_draft()
        if not result:
            await update.message.reply_text("No drafts found.")
            return
        draft_id, meta = result
        vid = meta.get("youtube_video_id", "none")
        await update.message.reply_text(
            f"📊 Latest draft: {meta.get('title','?')}\n"
            f"Status: {meta.get('status','?')}\n"
            f"YouTube: https://studio.youtube.com/video/{vid}/edit\n\n"
            f"Commands: publish · reject · regenerate · status"
        )

    else:
        await update.message.reply_text(
            "Commands:\n"
            "• /publish — make the latest video public\n"
            "• /reject — delete from YouTube & regenerate next run\n"
            "• /regenerate — keep on YouTube but create a new one\n"
            "• /status — show latest draft info",
            parse_mode="HTML",
        )


# ---- Smoke test: send a hello message ------------------------------------

def test_message() -> None:
    url = f"https://api.telegram.org/bot{_bot_token()}/sendMessage"
    r = httpx.post(url, json={"chat_id": _chat_id(),
                              "text": "✅ Pipeline online — Telegram bot reachable."}, timeout=10)
    print(r.status_code, r.text[:200])


def send_skip_day_alert(reason: str, details: str = "") -> None:
    """Sync helper: notify the user that today's run was halted to avoid
    shipping repeated content. Safe to call from the orchestrator; swallows
    Telegram errors so the pipeline exit code remains the source of truth.
    """
    try:
        text = (
            "<b>⚠️ Pipeline skipped today</b>\n\n"
            f"<b>Reason:</b> {reason}\n\n"
            + (f"<b>Details:</b>\n<i>{details[:600]}</i>\n\n" if details else "")
            + "Zero-repetition policy: rather than ship a clone, no video was produced.\n"
            "Check the banlists in <code>data/cache/trends/banlists/</code>."
        )
        url = f"https://api.telegram.org/bot{_bot_token()}/sendMessage"
        httpx.post(url, json={
            "chat_id": _chat_id(), "text": text, "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        log.warning("skip-day alert failed: %s", e)


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Swallow transient network errors (DNS hiccups, connection drops) without
    printing the whole stack — PTB retries polling automatically. We log a one-
    line warning so the user sees it happened without the noise.
    """
    err = ctx.error
    if err is None:
        return
    name = type(err).__name__
    msg = str(err) or "<no message>"
    # These are all transient — ignore cleanly
    TRANSIENT = ("ConnectError", "NetworkError", "TimedOut", "ReadTimeout",
                 "RemoteProtocolError", "ConnectTimeout")
    if name in TRANSIENT or any(t in msg for t in ("nodename", "Temporary failure", "timed out")):
        log.warning("telegram transient: %s: %s", name, msg)
        return
    log.error("telegram unhandled: %s: %s", name, msg)


def run_bot() -> None:
    app = Application.builder().token(_bot_token()).build()
    app.add_handler(CommandHandler("start", _on_start))
    # /publish, /reject, /regenerate, /status as proper slash commands
    app.add_handler(CommandHandler("publish", _on_text))
    app.add_handler(CommandHandler("reject", _on_text))
    app.add_handler(CommandHandler("regenerate", _on_text))
    app.add_handler(CommandHandler("regen", _on_text))
    app.add_handler(CommandHandler("status", _on_text))
    app.add_handler(CommandHandler("delete", _on_text))
    app.add_handler(CommandHandler("approve", _on_text))
    app.add_handler(CallbackQueryHandler(_on_callback))
    # Also handle plain text (without /) like "reject", "publish"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_error_handler(_on_error)
    log.info("Telegram bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="Send a hello message and exit.")
    p.add_argument("--run", action="store_true", help="Run the polling bot.")
    args = p.parse_args()
    if args.test:
        test_message()
        return
    if args.run:
        run_bot()
        return
    p.print_help()


if __name__ == "__main__":
    main()
