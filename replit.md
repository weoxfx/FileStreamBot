# Tsukuyomi Anime Bot

A Telegram bot that ingests anime video files, applies a "Tsukuyomi" watermark via ffmpeg, stores them in a Telegram dump channel, and serves a REST API for a streaming website.

## How It Works

1. **Upload** — An authorized user sends a video to the bot. Three modes available (switch with `/mode`):
   - **AniList ID mode** (default): caption `AniList ID | Episode | sub/dub/hsub | quality`
     Example: `21355 | 1 | sub | 720p`
   - **Auto Sub** / **Auto Dub**: filename parsed automatically
     Example filename: `ReZERO -Starting Life in Another World- - 1 - 360p.mkv`

2. **Watermark** — The bot downloads the video, burns the "Tsukuyomi" watermark (top-right, semi-transparent) using ffmpeg, and re-uploads to the dump channel.

3. **Index** — Metadata is stored in the site SQLite DB (`data/site.db`) keyed by AniList ID. The bot returns a secure stream token.

4. **Stream** — Your website calls `GET /stream/{token}` with the `X-API-Key` header to stream the video directly from Telegram.

## Required Environment Variables

Set these in the Replit Secrets tab (or in `.env` on your VPS):

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
GET /status                                    — Server health (no auth)
GET /api/anime                                 — List all anime
GET /api/anime/{anilist_id}                    — Anime detail + episode list
GET /api/episodes/{anilist_id}                 — All episodes for an anime
GET /api/episodes/{anilist_id}?episode=N       — All qualities for one episode
GET /api/qualities/{anilist_id}/{episode}      — Quality picker for one episode
GET /stream/{token}                            — Stream video (supports Range headers)
```

### Example: get all qualities for AniList ID 21355, Episode 1

```bash
curl -H "X-API-Key: your_key" \
  "https://your-bot.repl.co/api/episodes/21355?episode=1"
```

Response:
```json
{
  "anilist_id": 21355,
  "episode": 1,
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
| `/mode` | Switch upload mode (AniList ID / Auto Sub / Auto Dub) |
| `/stop` | Cancel all active uploads |
| `/status` | User + ban counts |
| `/ban <id>` | Ban a user |
| `/unban <id>` | Unban a user |
| `/logs` | Show last 20 bot log entries |
| `/apikey` | Show the current site API key |
| `/del <token>` | Delete an episode by stream token |
| `/broadcast` | (reply to a message) Broadcast to all users |

## Subtitle Upload

Send a `.vtt`, `.srt`, `.ass`, or `.ssa` file with this caption:
```
AniList ID | Episode | Language Label | lang_code
```
Example: `21355 | 1 | English | en`

The subtitle must be uploaded **after** the video for that episode.

## Running

The bot runs on port 5000 and serves both the Telegram bot and the HTTP API from the same process.

Start it with: `python3 -m FileStream`

Deployed on VPS via Docker: `docker compose up -d`

## User Preferences

- SQLite for both bot DB and site DB (separate files)
- Watermark: "Tsukuyomi" top-right, semi-transparent, ffmpeg re-encode
- Stream tokens are HMAC-signed and non-guessable
- Only the website (with the API key) can access stream URLs
- AniList ID-based indexing — each AniList entry = one series (season encoded in ID)
- Modes: `anilist_id` (default), `auto_sub`, `auto_dub` — stored in bot DB settings table
