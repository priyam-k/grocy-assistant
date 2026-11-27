"""FastAPI app: serves the chat page and streams assistant turns over SSE."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import anthropic
import httpx
import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlencode
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus

from . import config
from .agent import Agent, Emit
from .db import DB, Conversation
from .grocy import Catalog, Grocy, GrocyError, actor_email, actor_key
from .tools import Toolbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC = Path(__file__).resolve().parent.parent / "static"
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
TZ = ZoneInfo(config.TIMEZONE)

# Cloudflare and phones cache .js and .css for hours regardless of our headers. Every asset
# address therefore carries a hash of the static folder, so a deploy changes the address and
# nobody can end up with a new page running an old script.
VERSION = hashlib.sha1(b"".join(f.read_bytes() for f in sorted(STATIC.iterdir()) if f.is_file())).hexdigest()[:10]
INDEX_HTML = re.sub(r'(/static/[\w.-]+)"', rf'\1?v={VERSION}"', (STATIC / "index.html").read_text())


class State:
    grocy: Grocy
    catalog: Catalog
    toolbox: Toolbox
    db: DB
    agent: Agent
    stt: httpx.AsyncClient
    locks: dict[str, asyncio.Lock] = {}
    tasks: set[asyncio.Task] = set()
    jwks: jwt.PyJWKClient | None = None


S = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.grocy = Grocy(config.GROCY_URL, config.GROCY_API_KEY)
    S.catalog = Catalog(S.grocy)
    S.toolbox = Toolbox(S.grocy, S.catalog, today=lambda: datetime.now(TZ).date())
    S.db = DB()
    S.agent = Agent(anthropic.AsyncAnthropic(), S.db, S.toolbox)
    S.stt = httpx.AsyncClient(timeout=60)
    if config.CF_ACCESS_TEAM_DOMAIN and config.CF_ACCESS_AUD:
        S.jwks = jwt.PyJWKClient(f"https://{config.CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs", cache_keys=True)
    else:
        log.warning("CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD not set: trusting the Cloudflare email header without verifying it")
    try:
        await S.catalog.refresh()
        log.info("Connected to Grocy: %d products", len(S.catalog.products))
    except GrocyError as e:
        log.error("Grocy is not reachable yet: %s", e)
    yield
    await S.grocy.close()
    await S.stt.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------- auth

class User(BaseModel):
    email: str
    name: str


async def current_user(request: Request) -> User:
    return await identify(request.headers)


async def identify(headers) -> User:
    email = headers.get("cf-access-authenticated-user-email", "").strip().lower()
    if S.jwks:
        token = headers.get("cf-access-jwt-assertion", "")
        try:
            key = await asyncio.to_thread(S.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=config.CF_ACCESS_AUD,
                issuer=f"https://{config.CF_ACCESS_TEAM_DOMAIN}",
            )
            email = str(claims.get("email", "")).lower()
        except Exception:
            raise HTTPException(401, "Please sign in again.")
        if not email:
            raise HTTPException(401, "Please sign in again.")
    email = email or "family@local"
    name = config.USER_NAMES.get(email) or re.split(r"[._+\d]", email.split("@")[0])[0].capitalize() or "Family"
    return User(email=email, name=name)


# ---------------------------------------------------------------- helpers

def lock_for(conversation_id: str) -> asyncio.Lock:
    return S.locks.setdefault(conversation_id, asyncio.Lock())


def act_as(user: "User") -> None:
    """Changes made in this task are attributed to this person (see grocy.py)."""
    actor_email.set(user.email)
    actor_key.set(config.GROCY_USER_KEYS.get(user.email, ""))


def stream(conv: Conversation, user: "User", run: Callable[[Emit], Awaitable[None]], first: list[dict] | None = None) -> StreamingResponse:
    """Run a turn in its own task (so it finishes even if the phone drops off) and stream its events."""
    lock = lock_for(conv.id)
    if lock.locked():
        raise HTTPException(409, "Still working on your last message.")
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    for event in first or []:
        queue.put_nowait(event)

    async def worker() -> None:
        act_as(user)
        async with lock:
            try:
                await run(queue.put)
            except Exception:
                log.exception("turn failed for %s", conv.id)
                await queue.put({"type": "error", "message": "Something went wrong on my side. Please try again."})
            finally:
                await queue.put(None)

    task = asyncio.create_task(worker())
    S.tasks.add(task)
    task.add_done_callback(S.tasks.discard)

    async def events():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                yield 'data: {"type":"end"}\n\n'
                return
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def get_or_create(conversation_id: str | None, user: User) -> Conversation:
    if conversation_id:
        conv = S.db.get(conversation_id, user.email)
        if conv:
            return conv
    try:
        await S.catalog.refresh()
    except GrocyError as e:
        raise HTTPException(502, f"I can't reach Grocy right now ({e}).")
    return S.db.create(user.email, S.catalog.text())


# ----------------------------------------------------------------- routes

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/state")
async def state(user: User = Depends(current_user)):
    conv = S.db.latest(user.email, config.IDLE_HOURS * 3600)
    return {
        "user": user.model_dump(),
        "stt": "deepgram" if config.DEEPGRAM_API_KEY else "server" if config.STT_API_KEY else "browser",
        "grocy_url": config.GROCY_PUBLIC_URL,
        "history": S.db.history(user.email),
        "conversation": {"id": conv.id, "items": S.db.shown(conv.id)} if conv else None,
    }


@app.get("/api/conversations")
async def conversations(user: User = Depends(current_user)):
    return S.db.history(user.email)


@app.get("/api/conversations/{conversation_id}")
async def conversation(conversation_id: str, user: User = Depends(current_user)):
    conv = S.db.get(conversation_id, user.email)
    if not conv:
        raise HTTPException(404, "That chat no longer exists.")
    return {"id": conv.id, "items": S.db.shown(conv.id)}


@app.post("/api/chat")
async def chat(
    text: str = Form(""),
    voice: bool = Form(False),
    conversation_id: str = Form(""),
    images: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
):
    text = text.strip()[:8000]
    if not text and not images:
        raise HTTPException(400, "Empty message.")
    if len(images) > config.MAX_IMAGES:
        raise HTTPException(400, f"Please send at most {config.MAX_IMAGES} photos at a time.")
    if S.db.turns_today(user.email) >= config.DAILY_MESSAGE_LIMIT:
        raise HTTPException(429, "That's the limit for today. Please try again tomorrow.")

    blocks: list[dict] = []
    names: list[str] = []
    for image in images:
        ext = IMAGE_TYPES.get(image.content_type or "")
        data = await image.read()
        if not ext or len(data) > config.MAX_IMAGE_BYTES:
            raise HTTPException(400, "That photo could not be used. Please try another one.")
        name = f"{uuid.uuid4().hex}.{ext}"
        (config.UPLOAD_DIR / name).write_bytes(data)
        names.append(name)
        blocks.append({"type": "image", "source": {"type": "upload", "name": name, "media_type": image.content_type}})

    conv = await get_or_create(conversation_id or None, user)
    now = datetime.now(TZ)
    context = f"[{now:%A %-d %B %Y, %-I:%M %p} · from {user.name} · {'spoken' if voice else 'typed'}]"
    blocks.append({"type": "text", "text": f"{context}\n{text or '(photo attached, no text)'}"})
    shown = S.db.show(conv.id, {"t": "user", "text": text, "images": names, "voice": voice})
    S.db.set_title(conv.id, (text[:80] or "Photo").splitlines()[0])

    return stream(
        conv,
        user,
        lambda emit: S.agent.user_turn(conv, blocks, emit),
        first=[{"type": "start", "conversation_id": conv.id, "item": shown}],
    )


class ConfirmBody(BaseModel):
    conversation_id: str
    approved: list[str]


@app.post("/api/confirm")
async def confirm(body: ConfirmBody, user: User = Depends(current_user)):
    conv = S.db.get(body.conversation_id, user.email)
    if not conv:
        raise HTTPException(404, "Conversation not found.")
    return stream(conv, user, lambda emit: S.agent.confirm(conv, set(body.approved), emit))


class UndoBody(BaseModel):
    conversation_id: str
    card_id: int


@app.post("/api/undo")
async def undo(body: UndoBody, user: User = Depends(current_user)):
    conv = S.db.get(body.conversation_id, user.email)
    shown = S.db.shown(body.conversation_id, body.card_id) if conv else []
    if not conv or not shown or shown[0].get("t") != "card" or shown[0].get("undone"):
        raise HTTPException(404, "Nothing to undo.")
    card = shown[0]
    act_as(user)
    async with lock_for(conv.id):
        conv = S.db.get(body.conversation_id, user.email)
        undone, failed = [], []
        for act in card["actions"]:
            if not act.get("transaction_id") or not act.get("ok"):
                continue
            try:
                await S.grocy.post(f"/stock/transactions/{act['transaction_id']}/undo")
                undone.append(act["title"])
                S.db.audit(user.email, conv.id, act["title"], "Undone: " + act["detail"], {"ok": True, "transaction_id": act["transaction_id"]})
            except GrocyError as e:
                failed.append(f"{act['title']} ({e})")
        if undone:
            card["undone"] = True
            S.db.update_shown(body.card_id, card)
            conv.notes.append("[App note: the user tapped Undo, and these earlier stock changes were reversed: " + ", ".join(undone) + "]")
            S.db.save(conv)
    if failed and not undone:
        raise HTTPException(502, "Could not undo: " + "; ".join(failed))
    return {"card": card, "failed": failed}


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...), user: User = Depends(current_user)):
    if not config.STT_API_KEY:
        raise HTTPException(501, "Voice transcription is not set up.")
    data = await audio.read()
    if not data or len(data) > config.MAX_AUDIO_BYTES:
        raise HTTPException(400, "That recording could not be used.")
    # Whisper-style models take a short vocabulary hint; product names make food words land.
    vocabulary = ", ".join(p["name"] for p in S.catalog.products.values())[:800]
    form = {"model": config.STT_MODEL, "response_format": "json", "prompt": f"Kitchen and grocery update. {vocabulary}"}
    if config.STT_LANGUAGE:
        form["language"] = config.STT_LANGUAGE
    try:
        r = await S.stt.post(
            f"{config.STT_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.STT_API_KEY}"},
            data=form,
            files={"file": (audio.filename or "speech.webm", data, audio.content_type or "audio/webm")},
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.error("STT failed: %s %s", e.response.status_code, e.response.text[:300])
        raise HTTPException(502, "I couldn't understand the recording. Please try again.")
    except httpx.HTTPError:
        raise HTTPException(502, "I couldn't reach the transcription service. Please try again.")
    return {"text": str(r.json().get("text", "")).strip()}


@app.websocket("/api/listen")
async def listen(ws: WebSocket):
    """Live transcription: the page sends 16 kHz PCM, we relay it to Deepgram and pass the words back.

    The Deepgram key never leaves the server. The page sends the text "stop" when the person is
    done; Deepgram then flushes its last words and closes, which ends the relay.
    """
    try:
        await identify(ws.headers)
    except HTTPException:
        await ws.close(code=4401)
        return
    if not config.DEEPGRAM_API_KEY:
        await ws.close(code=4404)
        return
    await ws.accept()
    base = [
        ("model", config.DEEPGRAM_MODEL), ("language", config.DEEPGRAM_LANGUAGE),
        ("encoding", "linear16"), ("sample_rate", "16000"), ("channels", "1"),
        ("interim_results", "true"), ("smart_format", "true"), ("endpointing", "300"),
    ]
    # Product names as key terms, so "paneer" and "ghee" are not misheard.
    terms = [("keyterm", p["name"]) for p in list(S.catalog.products.values())[:100]]
    headers = {"Authorization": f"Token {config.DEEPGRAM_API_KEY}"}

    async def relay(params: list[tuple[str, str]]) -> None:
        async with ws_connect("wss://api.deepgram.com/v1/listen?" + urlencode(params), additional_headers=headers, open_timeout=8) as dg:
            async def upstream() -> None:
                try:
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect" or message.get("text") == "stop":
                            break
                        if message.get("bytes"):
                            await dg.send(message["bytes"])
                finally:
                    await dg.send(json.dumps({"type": "CloseStream"}))

            async def downstream() -> None:
                async for raw in dg:
                    data = json.loads(raw) if isinstance(raw, str) else {}
                    if data.get("type") != "Results":
                        continue
                    text = data["channel"]["alternatives"][0].get("transcript", "")
                    if text:
                        await ws.send_json({"text": text, "final": bool(data.get("is_final"))})

            sender = asyncio.create_task(upstream())
            try:
                await asyncio.wait_for(downstream(), timeout=180)
            finally:
                sender.cancel()

    try:
        try:
            await relay(base + terms)
        except InvalidStatus as e:
            if not terms or e.response.status_code != 400:
                raise
            log.warning("Deepgram refused the request with key terms (%s); retrying without", e.response.status_code)
            await relay(base)
    except Exception as e:
        log.error("Live transcription failed: %s: %s", type(e).__name__, e)
        try:
            await ws.send_json({"error": "I couldn't start listening. Please try again."})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


class LogBody(BaseModel):
    message: str


@app.post("/api/log")
async def client_log(body: LogBody, request: Request, user: User = Depends(current_user)):
    """Problems the page hits on someone's phone (mostly voice), so they can be diagnosed."""
    log.warning("client %s [%s]: %s", user.email, request.headers.get("user-agent", "")[:120], body.message[:300])
    return {"ok": True}


@app.get("/api/uploads/{name}")
async def upload(name: str, user: User = Depends(current_user)):
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp|gif)", name) or not (config.UPLOAD_DIR / name).exists():
        raise HTTPException(404)
    return FileResponse(config.UPLOAD_DIR / name, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/activity")
async def activity(user: User = Depends(current_user)):
    """Who changed what, newest first."""
    return S.db.audit_log()


@app.get("/api/usage")
async def usage(user: User = Depends(current_user)):
    return S.db.usage_summary()


class Versioned(StaticFiles):
    """Addresses with ?v= never change content, so they may be cached forever; others not at all."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-store"
        return response


@app.get("/")
async def index(user: User = Depends(current_user)):
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


app.mount("/static", Versioned(directory=STATIC), name="static")
