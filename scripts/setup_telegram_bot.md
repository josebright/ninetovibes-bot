# Telegram review bot — 2-minute setup (free)

## 1. Create the bot
1. Open Telegram on your phone or desktop.
2. Search for **@BotFather** (the official one, blue checkmark).
3. Send: `/newbot`
4. Bot name: `NineToVibes Reviewer`
5. Bot username (must end in `bot`): `ninetovibes_reviewer_bot` (or anything available)
6. BotFather replies with a **token** like `123456:ABCdef...` — copy it.
7. Paste into `.env` as `TELEGRAM_BOT_TOKEN=...`

## 2. Get your chat ID (so the bot knows where to send review pings)
1. Start a chat with your new bot: search its username → **Start**.
2. Send any message (e.g. "hi").
3. Open this URL in your browser, replacing `<TOKEN>`:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find `"chat":{"id": 123456789` in the JSON. That number is your chat ID.
5. Paste into `.env` as `TELEGRAM_CHAT_ID=...`

## 3. Test
```bash
cd /Users/uchennaugwumadu/Downloads/pipeline
.venv/bin/python -m src.notify.telegram_bot --test
```
You should receive a "Pipeline online ✅" message in the chat.

## What the bot does
- Pings you when a video is finished and in `data/drafts/`.
- Shows title, thumbnail, description, and the video as an attachment.
- Four buttons: **Approve & Publish**, **Reject**, **Regenerate**, **Reschedule**.
- Approval triggers the upload to YouTube (as public) with AI disclosure enabled.
