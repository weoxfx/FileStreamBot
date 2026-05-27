# Tsukuyomi Anime Bot

A Telegram bot that ingests anime video files, applies a "Tsukuyomi" watermark via ffmpeg, stores them in a Telegram dump channel, and serves a REST API for a streaming website.

## How It Works

1. **Upload** — An authorized user sends a video to the bot with a caption:
   ```
   Anime Name | Season | Episode | sub/dub/hsub | quality
   ```
   Example: `Naruto | 1 | 2 | sub | 720p`

2. **Watermark** — The bot downloads the video, burns the "Tsukuyomi" watermark (top-right, semi-transparent) using ffmpeg, and re-uploads to the dump channel.

3. **Index** — Metadata is stored in the site SQLite DB (`data/site.db`). The bot returns a secure stream token.

4. **Stream** — Your website calls `GET /stream/{token}` with the `X-API-Key` header to stream the video directly from Telegram.

## Required Environment Variables

Set these in the Replit Secrets tab:

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API Hash |
| `BOT_TOKEN` | Bot token from @BotFather |
| `OWNER_ID` | Your Telegram user ID |
| `DUMP_CHANNEL` | Private channel ID for storing watermarked videos (bot must be admin) |
| `FLOG_CHANNEL` | File log channel ID |
| `ULOG_CHANNEL` | User log channel ID |
| `SITE_API_KEY` | Secret key your website sends as `X-API-Key` header |
| `STREAM_SECRET` | Secret for signing stream tokens (min 32 chars) |

Optional: `AUTH_USERS`, `WORKERS`, `MULTI_TOKEN1`, `MULTI_TOKEN2`, ...

## Website API Reference

All endpoints require the header: `X-API-Key: <your SITE_API_KEY>`

```
GET /status                              — Server health (no auth)
GET /api/anime                           — List all anime
GET /api/anime/{slug}                    — Anime detail + season map
GET /api/episodes/{slug}                 — All episodes for an anime
GET /api/episodes/{slug}?season=1        — Episodes for a specific season
GET /api/episodes/{slug}?season=1&episode=2 — All qualities for one episode
GET /api/qualities/{slug}/{season}/{ep}  — Quality picker for one episode
GET /stream/{token}                      — Stream video (supports Range headers)
```

### Example: get all qualities for Naruto S1E2

```bash
curl -H "X-API-Key: your_key" \
  "https://your-bot.repl.co/api/episodes/naruto?season=1&episode=2"
```

Response:
```json
{
  "slug": "naruto",
  "season": 1,
  "episode": 2,
  "qualities": [
    { "audio_type": "dub", "quality": "1080p", "stream_token": "abc123...", "file_size": 1200000000 },
    { "audio_type": "sub", "quality": "720p",  "stream_token": "xyz789...", "file_size": 800000000 }
  ]
}
```

Your website renders a quality picker, then streams via:
```
https://your-bot.repl.co/stream/{token}
```
with the `X-API-Key` header. This supports `Range` headers for seeking.

## Bot Commands (Admin)

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/status` | User + ban counts |
| `/ban <id>` | Ban a user |
| `/unban <id>` | Unban a user |
| `/logs` | Show last 20 bot log entries |
| `/apikey` | Show the current site API key |
| `/broadcast` | (reply to a message) Broadcast to all users |

## Running

The bot runs on port 5000 and serves both the Telegram bot and the HTTP API from the same process.

Start it with: `python3 -m FileStream`

## User Preferences

- SQLite for both bot DB and site DB (separate files)
- Watermark: "Tsukuyomi" top-right, semi-transparent, ffmpeg re-encode
- Stream tokens are HMAC-signed and non-guessable
- Only the website (with the API key) can access stream URLs
