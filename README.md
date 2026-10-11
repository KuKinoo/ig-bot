# AI Influencer Bot — free edition

Automated Instagram news-carousel bot with **human approval in Telegram**, running on **$0 infrastructure**.

Every day it reads AI/tech news feeds, lets **Gemini** write the caption and slide text, lets **Cloudflare Workers AI (FLUX)** paint the backgrounds, composes branded 1080×1350 slides with **Pillow**, and sends the draft to your Telegram. You tap **✅ Approve & Publish** (posts to Instagram through the Graph API) or **❌ Reject / Edit** (you type feedback, the bot regenerates). Nothing is published without your tap.

| Job | Where it runs | Cost |
|---|---|---|
| Daily draft, publishing, regeneration | GitHub Actions (public repo) | free |
| Catching Telegram button taps | Cloudflare Worker (`worker/worker.js`) | free |
| State + draft images | `data` branch of this repo | free |
| Caption / slide text | Google Gemini API (free tier) | free |
| Background images | Unsplash photos (free API key), Cloudflare Workers AI – FLUX schnell (10,000 neurons/day) as fallback | free |

## How it works

```
                      07:00 UTC (cron)
GitHub Actions: daily.yml ──► RSS ► Gemini ► FLUX ► Pillow ► push images to `data` branch ► Telegram preview
                                                                                            │
                                                                              you tap Approve / Reject
                                                                                            ▼
Telegram ──webhook──► Cloudflare Worker ──repository_dispatch──► GitHub Actions: telegram.yml
                                                                  ├─ Approve ► Instagram Graph API (downloads images from raw.githubusercontent.com)
                                                                  └─ Reject  ► asks for feedback ► regenerates ► new preview
```

There is no always-on server. A tap takes roughly **20–60 seconds** to be answered because GitHub boots a fresh runner. The repository must be **public** so that Instagram can download the images and so that Actions minutes are free.

## Repository layout

```
.github/workflows/daily.yml     scheduled draft generation (cron, UTC)
.github/workflows/telegram.yml  handler for button taps / messages (repository_dispatch)
run_daily.py                    entry point of the daily job
handle_update.py                entry point of the Telegram job
config.py                       all settings, read from environment variables
services/                       scraper, ai_brain (Gemini), media (FLUX), template (Pillow),
                                telegram_bot, instagram_api, pipeline, storage, gitsync
scripts/data_branch.sh          pull/push of the `data` branch (state + images)
worker/worker.js                Cloudflare Worker: Telegram → GitHub relay
assets/fonts/                   Poppins (OFL)
```

## Setup

The complete illustrated, step-by-step manual (about 30 pages — architecture, accounts, every key, Worker deployment, webhook, key rotation, troubleshooting) is distributed as a separate PDF alongside this project. Short version:

1. **Repository** – public, containing these files; *Settings → Actions → General → Workflow permissions → Read and write*.
2. **Secrets** (*Settings → Secrets and variables → Actions*): `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID`, and (for free images) `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`.
3. **GitHub token for the relay** – fine-grained, only this repository, permission *Contents: Read and write*.
4. **Cloudflare Worker** `ig-bot-relay` – paste `worker/worker.js`; variables: `WEBHOOK_SECRET`, `TELEGRAM_CHAT_ID`, `TELEGRAM_BOT_TOKEN`, `GH_TOKEN` (secrets) and `GITHUB_REPO` = `owner/repo` (text).
5. **Telegram webhook** – run once:
   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://ig-bot-relay.<your-subdomain>.workers.dev" \
     -d "secret_token=<WEBHOOK_SECRET>"
   ```
   (`WEBHOOK_SECRET`: letters, digits, `_`, `-` only. Do not type the angle brackets.)
6. **Test** – send `/status` to the bot, then *Actions → Daily draft → Run workflow*.

## Real photos from Unsplash (optional, free)

With an `UNSPLASH_ACCESS_KEY` secret the bot illustrates posts with real photos:

1. Gemini writes one photo search query per slide; the bot fetches portrait candidates from Unsplash.
2. Gemini **looks at the candidates** and decides per slide: use as is, colour-grade, lightly AI-edit (img2img), or — if nothing fits — generate an AI picture instead (realistic style). No photo, no AI key: designed gradient.
3. In Telegram, tap **🖼 n** under the preview to see other candidates for picture *n* and pick one yourself (**More photos**, **🤖 AI image**, **🎨 Plain design** are there too). Nothing is published without your tap.

Guideline compliance (Unsplash API Guidelines): images are loaded from the URLs the API returns (`photo.urls`, only resized with supported parameters, `ixid` kept); each photo that goes into a published post is reported to `photo.links.download_location` on approval; the caption gets a photographer + Unsplash credit with `?utm_source=<UNSPLASH_APP_NAME>&utm_medium=referral` links and the slide shows a small "Photo: name / Unsplash" line. A demo key allows 50 API requests/hour (a draft uses about 5-8); the client stops before the limit.

Things to know: AI-edited photos are AI-generated content, so Instagram may ask for an "AI info" label; the free Cloudflare img2img model is low-resolution, so edits are rare (`MAX_PHOTO_EDITS`, default 2); never imply a photographer or person shown endorses the story. Variables: `UNSPLASH_APP_NAME`, `PHOTO_CANDIDATES`, `PHOTO_POOL`, `MAX_PHOTO_EDITS`, `REALISTIC_STYLE`.

## Configuration (optional repository *Variables*)

`BRAND_HANDLE`, `BRAND_KICKER`, `BRAND_ACCENT`, `IMAGE_STYLE`, `IMAGE_PROVIDER`, `RSS_FEEDS`, `MAX_SLIDE_IMAGES`, `GEMINI_TEXT_MODEL`, `GEMINI_FALLBACK_MODEL`, `INSTAGRAM_GRAPH_VERSION`. Defaults live in `config.py`. To change the posting time, edit the `cron` line in `daily.yml` (UTC).

## Commands

`/generate` new draft now · `/status` pending state · `/cancel` drop pending feedback · `/start` help

## Keeping it alive

| Credential | Lifetime | When it expires |
|---|---|---|
| `INSTAGRAM_ACCESS_TOKEN` | ≈ 60 days (long-lived user token) | publishing fails → renew and replace the secret |
| `GH_TOKEN` (Worker) | as chosen when created (max ≈ 1 year) | buttons stop working → create a new token, rotate the Worker secret |
| Other keys | no fixed expiry | rotate if leaked |

If a key leaks: **revoke it at the provider first**, then replace it. Never commit a real `.env` file. Scheduled workflows in public repos can be paused by GitHub after long inactivity — the daily job re-enables itself; otherwise use *Actions → Daily draft → Enable workflow* or `/generate`.

## Privacy note

Draft images and `state.json` live on the public `data` branch until a post is published. Do not draft anything confidential.

## Local experiments

Copy `.env.example` to `.env`, fill in the keys, then `python run_daily.py` sends a draft to Telegram. Publishing only works from GitHub Actions (images must be publicly reachable).
