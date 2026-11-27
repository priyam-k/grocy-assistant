"""The manual tool-use loop around the Anthropic Messages API.

History is strictly append-only and the system prompt is frozen per conversation:
current Claude models tie their thinking blocks to the exact prefix that produced
them, and an unchanged prefix is also what makes prompt caching hit.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

import anthropic

from . import config
from .db import DB, Conversation
from .grocy import GrocyError
from .prompt import system_blocks
from .tools import BY_NAME, Toolbox, ToolError

log = logging.getLogger("agent")
Emit = Callable[[dict], Awaitable[None]]

NOT_CONFIRMED = "Not done. The user did not confirm this; they replied instead. Read their message below."


def serialize(block: Any) -> dict:
    """An assistant content block as the plain dict we store and send back unchanged."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return block.model_dump(mode="json", exclude_none=True)


def hydrate(messages: list[dict]) -> list[dict]:
    """Swap stored photo references for the base64 image blocks the API expects."""
    out = []
    for m in messages:
        content = m["content"]
        if m["role"] == "user" and isinstance(content, list) and any(b.get("type") == "image" for b in content):
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": b["source"]["media_type"],
                        "data": base64.standard_b64encode((config.UPLOAD_DIR / b["source"]["name"]).read_bytes()).decode(),
                    },
                }
                if b.get("type") == "image"
                else b
                for b in content
            ]
        out.append({"role": m["role"], "content": content})
    return out


def result_block(tool_use_id: str, content: Any, is_error: bool = False) -> dict:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return block


def for_model(act: dict, result: dict | None) -> dict:
    row: dict[str, Any] = {"item": act["title"]}
    if result is None:
        row["status"] = "skipped: the user unticked this line"
    elif result["ok"]:
        row.update({"status": "done", **{k: v for k, v in result.items() if k != "ok"}})
    else:
        row.update({"status": "failed", "error": result["error"]})
    return row


def for_card(act: dict, act_id: str, result: dict | None = None, skipped: bool = False) -> dict:
    row = {"id": act_id, "kind": act["kind"], "title": act["title"], "detail": act["detail"]}
    if skipped:
        row["skipped"] = True
    elif result is not None:
        row["ok"] = result["ok"]
        if not result["ok"]:
            row["error"] = result["error"]
        if result.get("transaction_id"):
            row["transaction_id"] = result["transaction_id"]
    return row


class Agent:
    def __init__(self, client: anthropic.AsyncAnthropic, db: DB, toolbox: Toolbox):
        self.client = client
        self.db = db
        self.toolbox = toolbox
        self._specs: list[dict] | None = None

    def specs(self) -> list[dict]:
        if self._specs is None:
            self._specs = [t.spec() for t in self.toolbox.enabled()]
        return self._specs

    # -------------------------------------------------------- entry points

    async def user_turn(self, conv: Conversation, blocks: list[dict], emit: Emit) -> None:
        """A new message from the person. Settles anything still waiting for them first."""
        content: list[dict] = []
        if conv.pending:
            content += self._abandon_pending(conv)
        if conv.notes:
            blocks = [{"type": "text", "text": "\n".join(conv.notes)}] + blocks
            conv.notes = []
        conv.messages.append({"role": "user", "content": content + blocks})
        self.db.save(conv)
        await self._loop(conv, emit, new_turn=True)

    async def confirm(self, conv: Conversation, approved: set[str], emit: Emit) -> None:
        """The person tapped confirm (approved = ticked action ids) or cancel (empty set)."""
        pending = conv.pending
        if not pending or not pending.get("gated"):
            await emit({"type": "error", "message": "That request is no longer waiting. Nothing was changed."})
            return
        results = dict(pending["ready"])
        card_rows = []
        for call_index, call in enumerate(pending["gated"]):
            rows = []
            for i, act in enumerate(call["actions"]):
                act_id = f"{call_index}:{i}"
                if act_id in approved:
                    await emit({"type": "tool", "label": f"Saving {act['title']}"})
                    result = await self.toolbox.execute(act)
                    rows.append(for_model(act, result))
                    card_rows.append(for_card(act, act_id, result))
                else:
                    rows.append(for_model(act, None))
                    card_rows.append(for_card(act, act_id, skipped=True))
            if approved:
                results[call["tool_use_id"]] = result_block(call["tool_use_id"], {"results": rows})
            else:
                results[call["tool_use_id"]] = result_block(call["tool_use_id"], "The user tapped Cancel. Nothing was changed.")
        card = {"t": "card", "id": pending["card_id"], "state": "done" if approved else "cancelled", "actions": card_rows}
        self.db.update_shown(pending["card_id"], card)
        await emit({"type": "item", "item": card})
        conv.messages.append({"role": "user", "content": [results[i] for i in pending["order"]]})
        conv.pending = None
        self.db.save(conv)
        await self._loop(conv, emit, new_turn=False)

    def _abandon_pending(self, conv: Conversation) -> list[dict]:
        """The person typed instead of tapping: answer the waiting tool calls accordingly."""
        pending = conv.pending
        results = dict(pending["ready"])
        for call in pending.get("gated", []):
            results[call["tool_use_id"]] = result_block(call["tool_use_id"], NOT_CONFIRMED)
        if pending.get("gated"):
            shown = self.db.shown(conv.id, pending["card_id"])
            if shown:
                self.db.update_shown(pending["card_id"], {**shown[0], "state": "cancelled"})
        if pending.get("ask"):
            results[pending["ask"]["tool_use_id"]] = result_block(pending["ask"]["tool_use_id"], "The user's answer is in their message below.")
            shown = self.db.shown(conv.id, pending["ask"]["display_id"])
            if shown:
                self.db.update_shown(pending["ask"]["display_id"], {**shown[0], "answered": True})
        conv.pending = None
        return [results[i] for i in pending["order"]]

    # ------------------------------------------------------------ the loop

    async def _loop(self, conv: Conversation, emit: Emit, new_turn: bool) -> None:
        for _ in range(config.MAX_STEPS):
            try:
                message = await self._call_model(conv, emit)
            except anthropic.RateLimitError:
                await emit({"type": "error", "message": "I'm getting too many requests right now. Please try again in a minute."})
                return
            except anthropic.AuthenticationError:
                await emit({"type": "error", "message": "The assistant's API key is not working. Please tell Priyam."})
                return
            except anthropic.APIStatusError as e:
                log.error("Anthropic API error %s: %s", e.status_code, e.message)
                await emit({"type": "error", "message": "The assistant is having trouble right now. Please try again in a moment."})
                return
            except anthropic.APIConnectionError:
                await emit({"type": "error", "message": "I couldn't reach the assistant service. Please check the connection and try again."})
                return

            self.db.record_usage(conv.user, config.CLAUDE_MODEL, message.usage, new_turn)
            new_turn = False
            await emit({"type": "text_end"})

            if message.stop_reason == "refusal":
                await emit({"type": "error", "message": "I can't help with that one. Try asking in a different way."})
                return
            blocks = [serialize(b) for b in message.content]
            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            if message.stop_reason == "max_tokens" and tool_uses:
                await emit({"type": "error", "message": "That was too much to handle at once. Please send it in smaller parts."})
                return
            if not blocks:
                return

            conv.messages.append({"role": "assistant", "content": blocks})
            text = "".join(b["text"] for b in blocks if b["type"] == "text").strip()
            if text:
                self.db.show(conv.id, {"t": "assistant", "text": text})
            if not tool_uses:
                self.db.save(conv)
                return

            if await self._run_tools(conv, tool_uses, emit):
                return  # waiting for the person
        await emit({"type": "error", "message": "That took more steps than I expected, so I stopped. Please check what changed and try again."})

    async def _call_model(self, conv: Conversation, emit: Emit, retried: bool = False) -> Any:
        try:
            async with self.client.messages.stream(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=system_blocks(conv.catalog),
                tools=self.specs(),
                messages=hydrate(conv.messages),
                cache_control={"type": "ephemeral"},
                output_config={"effort": config.CLAUDE_EFFORT},
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        await emit({"type": "text", "text": event.text})
                return await stream.get_final_message()
        except anthropic.BadRequestError as e:
            # Thinking blocks are bound to the exact prompt and tool list that produced them.
            # After a deploy that changes either, old conversations fail this check; dropping
            # every thinking block is always allowed and lets the conversation continue.
            if retried or "thinking" not in str(e.message).lower():
                raise
            log.warning("Dropping thinking blocks for %s after: %s", conv.id, e.message)
            for m in conv.messages:
                if m["role"] == "assistant":
                    m["content"] = [b for b in m["content"] if b["type"] not in ("thinking", "redacted_thinking")]
            conv.messages = [m for m in conv.messages if m["content"]]
            return await self._call_model(conv, emit, retried=True)

    async def _run_tools(self, conv: Conversation, tool_uses: list[dict], emit: Emit) -> bool:
        """Run what can run now. Returns True when the turn must wait for the person."""
        results: dict[str, dict] = {}
        order = [u["id"] for u in tool_uses]
        ask_call = next((u for u in tool_uses if u["name"] == "ask_user"), None)

        async def read(use: dict) -> None:
            tool = BY_NAME[use["name"]]
            await emit({"type": "tool", "label": tool.label})
            try:
                results[use["id"]] = result_block(use["id"], await self.toolbox.read(use["name"], use["input"] or {}))
            except (ToolError, GrocyError) as e:
                results[use["id"]] = result_block(use["id"], str(e), is_error=True)
            except Exception as e:
                log.exception("read tool %s failed", use["name"])
                results[use["id"]] = result_block(use["id"], f"Tool failed: {type(e).__name__}: {e}", is_error=True)

        known = [u for u in tool_uses if u["name"] in BY_NAME and isinstance(u["input"], dict)]
        for use in tool_uses:
            if use not in known:
                results[use["id"]] = result_block(use["id"], f"Unknown tool or invalid input: {use['name']}", is_error=True)
        await asyncio.gather(*(read(u) for u in known if BY_NAME[u["name"]].kind == "read"))

        gated: list[dict] = []
        auto_rows: list[dict] = []
        for use in known:
            tool = BY_NAME[use["name"]]
            if tool.kind not in ("auto", "gated"):
                continue
            if ask_call and tool.kind == "gated":
                results[use["id"]] = result_block(use["id"], "Not done: you asked the user a question in the same step. Wait for the answer, then call this again.", is_error=True)
                continue
            await emit({"type": "tool", "label": tool.label})
            try:
                actions = await self.toolbox.plan(use["name"], use["input"])
            except (ToolError, GrocyError) as e:
                results[use["id"]] = result_block(use["id"], str(e), is_error=True)
                continue
            except Exception as e:
                log.exception("planning %s failed", use["name"])
                results[use["id"]] = result_block(use["id"], f"Tool failed: {type(e).__name__}: {e}", is_error=True)
                continue
            if tool.kind == "gated":
                gated.append({"tool_use_id": use["id"], "name": use["name"], "actions": actions})
            else:
                rows = []
                for i, act in enumerate(actions):
                    result = await self.toolbox.execute(act)
                    rows.append(for_model(act, result))
                    auto_rows.append(for_card(act, f"a{len(auto_rows)}", result))
                results[use["id"]] = result_block(use["id"], {"results": rows})

        if auto_rows:
            await emit({"type": "item", "item": self.db.show(conv.id, {"t": "card", "state": "done", "auto": True, "actions": auto_rows})})

        pending: dict[str, Any] = {"order": order, "ready": results, "gated": gated, "ask": None}
        if gated:
            card = {
                "t": "card",
                "state": "pending",
                "actions": [for_card(act, f"{ci}:{i}") for ci, call in enumerate(gated) for i, act in enumerate(call["actions"])],
            }
            card = self.db.show(conv.id, card)
            pending["card_id"] = card["id"]
            await emit({"type": "item", "item": card})
        if ask_call:
            inp = ask_call["input"] if isinstance(ask_call["input"], dict) else {}
            options = [str(o) for o in (inp.get("options") or [])][:5]
            item = self.db.show(conv.id, {"t": "ask", "question": str(inp.get("question", "")), "options": options})
            pending["ask"] = {"tool_use_id": ask_call["id"], "display_id": item["id"]}
            await emit({"type": "item", "item": item})

        if gated or ask_call:
            conv.pending = pending
            self.db.save(conv)
            return True
        conv.messages.append({"role": "user", "content": [results[i] for i in order]})
        self.db.save(conv)
        return False
