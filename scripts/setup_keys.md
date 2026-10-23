# API Keys — where to get each (all free)

Fill these in `.env` after running `scripts/setup.sh`.

## PEXELS_API_KEY (video B-roll)
1. Go to https://www.pexels.com/api/
2. Click "Get Started" → create a free account
3. Copy the API key shown on your dashboard
4. Paste into `.env` as `PEXELS_API_KEY=...`

Free tier: 200 requests/hour, 20,000/month. More than enough.

## PIXABAY_API_KEY (backup B-roll)
1. Go to https://pixabay.com/api/docs/
2. Sign up (free)
3. The key is shown at the top of the docs page once logged in
4. Paste into `.env` as `PIXABAY_API_KEY=...`

Free tier: 100 requests per 60s.

## YOUTUBE (upload)
See `setup_youtube_oauth.md` — it's a multi-step click-through, covered there.

## TELEGRAM (review/approve)
See `setup_telegram_bot.md` — two-minute setup.

## SEMANTIC_SCHOLAR_API_KEY (optional, free)
- Request at https://www.semanticscholar.org/product/api
- Works without a key at lower rate limits. Leave blank if you don't want to wait.
