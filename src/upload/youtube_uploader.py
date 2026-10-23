"""YouTube Data API v3 uploader.

Pipeline guardrails baked in:
  - Every upload starts as `privacy_status = "private"`. The video only goes
    public when the human reviewer presses "Approve & Publish" in the Telegram
    bot, which re-calls this module with privacy_status="public".
  - `containsSyntheticMedia` is always True — matches the "Altered or
    synthetic content" disclosure that YouTube requires for AI-generated or
    -altered content.
  - Category defaults per niche come from config/settings.yaml.

CLI:
  python -m src.upload.youtube_uploader --auth
      Run the OAuth browser flow once, caches a refresh token in
      secrets/youtube_token.json. After that the pipeline refreshes silently.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from src.core.config import env, settings
from src.core.logging import get_logger
from src.core.models import Niche, VideoDraft
from src.core.paths import ROOT

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


def _client_secrets_path() -> Path:
    p = env("YOUTUBE_CLIENT_SECRETS_FILE", "./secrets/youtube_client_secret.json")
    return (ROOT / p) if not Path(p).is_absolute() else Path(p)


def _token_path() -> Path:
    p = env("YOUTUBE_TOKEN_FILE", "./secrets/youtube_token.json")
    return (ROOT / p) if not Path(p).is_absolute() else Path(p)


def _load_credentials(interactive: bool = False) -> Credentials:
    token_path = _token_path()
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
        return creds

    if not interactive:
        raise RuntimeError(
            "No valid YouTube credentials. Run: python -m src.upload.youtube_uploader --auth"
        )

    secrets = _client_secrets_path()
    if not secrets.exists():
        raise RuntimeError(
            f"Missing {secrets}. Follow scripts/setup_youtube_oauth.md to download it."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    log.info("YouTube OAuth: token saved to %s", token_path)
    return creds


def _client(interactive: bool = False):
    creds = _load_credentials(interactive=interactive)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_as_private_draft(draft: VideoDraft) -> str:
    """Upload a VideoDraft as a private draft. Returns the YouTube video ID."""
    s = settings()["upload"]
    cat = s["category_id_by_niche"].get(draft.niche.value, "27")

    # YouTube auto-detects Shorts from aspect ratio + duration. No #Shorts needed.
    # Keep title under 40 chars visible (mobile truncation). Use niche hashtags
    # in description (first 3 appear above the title as clickable links).
    title = draft.script.title[:70]

    # Build SEO-optimized description
    script_desc = draft.script.description or ""
    # First 125 chars = preview zone (visible before "more")
    hashtag_block = "\n\n#PersonalFinance #MoneyTips #NineToVibes #FinancialFreedom #WealthBuilding"
    cta_block = "\n\nSubscribe for daily money stories that actually teach you something."
    ai_block = (
        "\n\nAI disclosure: This video uses AI-generated characters and voice. "
        "Financial facts are sourced from the links below."
    )
    sources = "\n\nSources:\n" + "\n".join(f"- {u}" for u in draft.script.sources_used) if draft.script.sources_used else ""
    description = (script_desc + hashtag_block + cta_block + sources + ai_block)[:4950]

    tags = [
        "personal finance", "money tips", "financial literacy", "budgeting",
        "saving money", "investing for beginners", "wealth building",
        "money story", "NineToVibes",
    ]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": cat,
            "defaultLanguage": settings()["channel"]["language"],
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": bool(s.get("made_for_kids", False)),
            "containsSyntheticMedia": True,
        },
    }
    media = MediaFileUpload(draft.video_path, mimetype="video/mp4",
                            chunksize=8 * 1024 * 1024, resumable=True)
    log.info("uploading %s (%s) as private draft...", draft.id, draft.script.title)
    req = _client().videos().insert(
        part="snippet,status", body=body, media_body=media,
        notifySubscribers=False,
    )
    response = None
    while response is None:
        try:
            _, response = req.next_chunk()
        except HttpError as e:
            log.error("YouTube upload error: %s", e)
            raise
    video_id = response["id"]
    log.info("uploaded: https://studio.youtube.com/video/%s/edit", video_id)
    return video_id


def publish(video_id: str) -> None:
    """Flip a previously-uploaded private video to public.
    Called by the Telegram approval flow; keeps the 'publish' action explicit.
    """
    body = {
        "id": video_id,
        "status": {
            "privacyStatus": "public",
            "containsSyntheticMedia": True,
            "selfDeclaredMadeForKids": False,
        },
    }
    log.info("publishing %s...", video_id)
    _client().videos().update(part="status", body=body).execute()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", action="store_true", help="Run OAuth flow to save a token.")
    parser.add_argument("--check", action="store_true", help="Verify existing token is valid.")
    args = parser.parse_args()
    if args.auth:
        _load_credentials(interactive=True)
        print("✅ YouTube OAuth token saved.")
        return
    if args.check:
        yt = _client()
        r = yt.channels().list(part="snippet", mine=True).execute()
        ch = r.get("items", [{}])[0].get("snippet", {})
        print(f"✅ Authenticated as: {ch.get('title')!r}  (channel: {ch.get('customUrl','?')})")
        return
    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
