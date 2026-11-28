# Pantry: a family chat assistant for Grocy

A mobile-first chat page where someone types, talks or sends a receipt photo, and Claude reads and
updates a self-hosted [Grocy](https://grocy.info) for them.

## How it works

- `app/main.py`: FastAPI. Serves the page and streams each assistant turn over SSE.
- `app/agent.py`: manual tool-use loop on the Anthropic Messages API (streaming, prompt caching).
- `app/tools.py`: the tools, thin wrappers over Grocy's REST API.
- `app/grocy_guide.md`: reference text about Grocy's own features, returned by the `grocy_help`
  tool so the assistant can answer "can Grocy do X" and "where do I find Y".
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
menu, Manage API keys).

Voice has three engines, picked by which key is set:

- `DEEPGRAM_API_KEY`: live transcription. The page streams 16 kHz audio over `/api/listen`, the
  server relays it to Deepgram, and words appear while the person speaks. `DEEPGRAM_LANGUAGE=multi`
  handles Hindi and English in one sentence. Product names are sent as key terms.
- `STT_API_KEY`: any OpenAI-compatible `/audio/transcriptions` endpoint. Words appear after ✓.

✓ ends listening and leaves the words in the message box; nothing is sent until the send button is tapped.
- Neither: the phone's built-in speech recognition.

The microphone is opened on tap and released as soon as listening ends, including when the page is
hidden or the phone is locked.

## The page

- **A dropped connection does not lose a turn.** The server finishes a turn on its own; if the
  stream is cut (locked phone, weak signal) the page polls `/api/conversations/{id}` until `busy`
  is false and redraws from the server. A message that never reached the server goes back in the
  box.
- On phones the menu follows a sideways swipe on the chat. "Sign out" (two taps) goes to
  Cloudflare Access's `/cdn-cgi/access/logout` and only shows for people who came in through Access.
- Chats can be renamed and deleted from the ⋯ button in the menu (`PATCH` / `DELETE
  /api/conversations/{id}`). Deleting removes the chat and its photos; the audit log is kept.
- Appearance (theme, background tone, accent colour, text size) is stored per device in
  `localStorage` and applied as `data-*` attributes on `<html>`; all colours are CSS variables in
  `app.css`.
- The page is sized to `visualViewport` so the message box sits on top of the iOS keyboard.

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

## Who changed what

Grocy books every API change under the owner of the API key, so by default its journal shows one
user for everything. Three things identify the real person:

- `GET /api/activity` lists every change the assistant made with the signed-in email.
- Purchases and stock corrections carry a note on the stock entry: `via Pantry: <email>`.
  (Grocy's consume and open calls accept no note.)
- Optionally set `GROCY_USER_KEYS=email=key,...` with an API key created by each person's own
  Grocy user. Their changes then appear under their name in Grocy's own journal.

`GROCY_PUBLIC_URL` adds an "Open Grocy" link to the menu.
