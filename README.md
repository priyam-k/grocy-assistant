# Pantry: a family chat assistant for Grocy

A mobile-first chat page where someone types, talks or sends a receipt photo, and Claude reads and
updates a self-hosted [Grocy](https://grocy.info) for them.

## How it works

- `app/main.py`: FastAPI. Serves the page and streams each assistant turn over SSE.
- `app/agent.py`: manual tool-use loop on the Anthropic Messages API (streaming, prompt caching).
- `app/tools.py`: the tools, thin wrappers over Grocy's REST API.
- `app/grocy.py`: Grocy client and a cached copy of products, units, locations and stores.
- `app/db.py`: SQLite in `data/` for conversations, what the UI shows, and usage counts.
- `static/`: plain HTML/CSS/JS, no build step.

Things worth knowing before changing it:

- **Stock changes need a tap.** Tools marked `gated` are turned into a list of readable actions and
  held until the user confirms the card. Shopping list, meal plan, chore and task tools run
  immediately. This is enforced in `agent.py`, not left to the model.
- **History is append-only and the system prompt is frozen per conversation.** Current Claude models
  reject a conversation whose earlier turns, tools or system prompt changed. The product catalog is
  therefore snapshotted when a conversation starts. After a deploy that changes the prompt or tools,
  old conversations drop their thinking blocks once and carry on.
- **No deletes of master data.** The assistant can create and edit products, units, locations and
  stores, but cannot delete them.
- A conversation idle for `IDLE_HOURS` (default 6) is left behind and a fresh one starts.

## Configuration

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` and `GROCY_API_KEY` (Grocy: wrench
menu, Manage API keys). Voice transcription needs `STT_API_KEY` for any OpenAI-compatible
`/audio/transcriptions` endpoint; without it the page uses the phone's built-in recognition.

Set `CF_ACCESS_TEAM_DOMAIN` (e.g. `yourteam.cloudflareaccess.com`) and `CF_ACCESS_AUD` (the
Application Audience tag of the Access app) so every request's Access token is verified. Without
them the app trusts the `Cf-Access-Authenticated-User-Email` header as sent.

## Run

```sh
# on the VM, in ~/stacks/grocy-assistant
docker compose up -d --build
```

The container listens on `127.0.0.1:8484`. Point the Cloudflare Tunnel route for
`pantry.priyamkabra.com` at `http://localhost:8484`.

Local development:

```sh
uv venv --python 3.13 .venv && uv pip install -p .venv -r requirements.txt pytest
set -a; . ./.env; set +a
GROCY_URL=http://docker:9283 .venv/bin/uvicorn app.main:app --port 8484 --reload
.venv/bin/python -m pytest -q
```

Token usage per day, user and model: `GET /api/usage`.
