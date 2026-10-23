# YouTube Data API v3 — OAuth setup (free)

You only do this once. Takes about 10 minutes.

## 1. Create a Google Cloud project
1. Sign in to Google with the account that owns **@ninetovibes**.
2. Go to https://console.cloud.google.com/
3. Top bar → project dropdown → **New Project**. Name it `ninetovibes-pipeline`. Create.

## 2. Enable the YouTube Data API v3
1. Left menu → **APIs & Services** → **Library**.
2. Search "YouTube Data API v3" → click it → **Enable**.

## 3. Configure OAuth consent screen
1. Left menu → **APIs & Services** → **OAuth consent screen**.
2. User Type: **External** → Create.
3. Fill in:
   - App name: `NineToVibes Pipeline`
   - User support email: your email
   - Developer contact: your email
4. Save and continue through the scopes page (no changes needed).
5. **Test users** → Add the Google account that owns the YouTube channel. Save.
6. You can leave the app in "Testing" mode — no verification needed for personal use.

## 4. Create OAuth credentials
1. Left menu → **APIs & Services** → **Credentials**.
2. **+ Create Credentials** → **OAuth client ID**.
3. Application type: **Desktop app**. Name: `pipeline-uploader`.
4. **Download JSON**.
5. Save it to `secrets/youtube_client_secret.json` in this repo. (Create the `secrets/` folder if missing — `setup.sh` already does.)

## 5. First-time auth (opens a browser)
After you've filled `.env` and installed deps:

```bash
cd /Users/uchennaugwumadu/Downloads/pipeline
.venv/bin/python -m src.upload.youtube_uploader --auth
```

It opens a browser, you approve the scope (manage your YouTube account), and a
token is saved to `secrets/youtube_token.json`. The pipeline refreshes this
token automatically on every run.

## Cost
Zero. Free quota = 10,000 units/day. Each upload ≈ 1,600 units → ~6 uploads/day ceiling.
Our cadence (3/day) fits comfortably.
