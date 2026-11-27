"""End-to-end tests of the tool layer and the agent loop against a fake Grocy and a scripted model."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.agent import Agent
from app.db import DB
from app.grocy import Catalog, Grocy
from app.tools import Toolbox, ToolError


class FakeGrocy:
    """Just enough of Grocy's API, in memory."""

    def __init__(self):
        self.products = {
            1: {"id": 1, "name": "Milk", "location_id": 1, "qu_id_stock": 2, "qu_id_purchase": 2, "default_best_before_days": 7, "min_stock_amount": 1, "active": 1},
            2: {"id": 2, "name": "Eggs", "location_id": 1, "qu_id_stock": 1, "qu_id_purchase": 1, "default_best_before_days": 21, "min_stock_amount": 0, "active": 1},
            3: {"id": 3, "name": "Fennel seeds", "location_id": 2, "qu_id_stock": 3, "qu_id_purchase": 3, "default_best_before_days": -1, "min_stock_amount": 0, "active": 1},
        }
        self.stock = {1: 2.0, 2: 6.0}
        self.shopping: dict[int, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.tx = 0
        self.undone: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        ok = lambda data=None: httpx.Response(200, json=data) if data is not None else httpx.Response(204)
        tables = {
            "products": list(self.products.values()),
            "quantity_units": [{"id": 1, "name": "piece", "name_plural": "pieces"}, {"id": 2, "name": "bottle", "name_plural": "bottles"}, {"id": 3, "name": "pack", "name_plural": "packs"}],
            "locations": [{"id": 1, "name": "Fridge"}, {"id": 2, "name": "Pantry"}],
            "shopping_locations": [{"id": 1, "name": "Costco"}],
            "product_groups": [],
            "shopping_lists": [{"id": 1, "name": "Shopping list"}],
            "shopping_list": list(self.shopping.values()),
        }
        if m := re.fullmatch(r"/objects/(\w+)", path):
            entity = m[1]
            if request.method == "GET":
                return ok(tables[entity])
            if entity == "products":
                new_id = max(self.products) + 1
                self.products[new_id] = {"id": new_id, "active": 1, **body}
                return ok({"created_object_id": new_id})
            if entity == "shopping_list":
                new_id = len(self.shopping) + 1
                self.shopping[new_id] = {"id": new_id, "product_id": None, "done": 0, **body}
                return ok({"created_object_id": new_id})
        if path == "/system/config":
            return ok({"CURRENCY": "USD", "FEATURE_FLAG_CHORES": False, "FEATURE_FLAG_TASKS": False})
        if path == "/stock":
            return ok([{"product_id": p, "amount": a, "amount_opened": 0, "best_before_date": "2026-12-01"} for p, a in self.stock.items() if a > 0])
        if m := re.fullmatch(r"/stock/products/(\d+)", path):
            pid = int(m[1])
            return ok({"product": self.products[pid], "stock_amount": self.stock.get(pid, 0), "stock_amount_opened": 0})
        if m := re.fullmatch(r"/stock/products/(\d+)/(add|consume|open)", path):
            pid, op = int(m[1]), m[2]
            if op == "consume" and body["amount"] > self.stock.get(pid, 0):
                return httpx.Response(400, json={"error_message": "Amount to be consumed cannot be > current stock amount"})
            self.stock[pid] = self.stock.get(pid, 0) + (body["amount"] if op == "add" else -body["amount"] if op == "consume" else 0)
            self.tx += 1
            return ok([{"transaction_id": f"tx{self.tx}", "product_id": pid, "amount": body["amount"]}])
        if path == "/stock/shoppinglist/add-product":
            new_id = len(self.shopping) + 1
            self.shopping[new_id] = {"id": new_id, "product_id": body["product_id"], "amount": body.get("product_amount", 1), "shopping_list_id": 1, "done": 0}
            return ok()
        if path == "/stock/shoppinglist/remove-product":
            return ok()
        if m := re.fullmatch(r"/stock/transactions/(\w+)/undo", path):
            self.undone.append(m[1])
            return ok()
        return httpx.Response(404, json={"error_message": f"not faked: {request.method} {path}"})


@pytest.fixture
def world(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    fake = FakeGrocy()
    grocy = Grocy("http://grocy", "key", transport=httpx.MockTransport(fake.handler))
    catalog = Catalog(grocy)
    toolbox = Toolbox(grocy, catalog, today=lambda: date(2026, 11, 27))
    return SimpleNamespace(fake=fake, grocy=grocy, catalog=catalog, toolbox=toolbox, db=DB(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- tool layer

def test_catalog_text_lists_products(world):
    run(world.catalog.refresh())
    text = world.catalog.text()
    assert "3 | Fennel seeds | pack | Pantry" in text
    assert "1=Fridge" in text
    assert not world.catalog.feature("FEATURE_FLAG_CHORES")
    assert "get_chores" not in [t.name for t in world.toolbox.enabled()]


def test_purchase_plan_and_execute(world):
    async def go():
        actions = await world.toolbox.plan("add_purchases", {"store_id": 1, "items": [
            {"product_id": 1, "amount": 2, "total_price": 7},
            {"new_product": {"name": "Walnuts", "location_id": 2, "qu_id": 3}, "amount": 1},
        ]})
        assert actions[0]["title"] == "Milk"
        assert actions[0]["detail"] == "+2 bottles · $7.00 · best before Dec 4 · Fridge"
        assert actions[0]["args"]["price"] == 3.5
        assert actions[1]["kind"] == "new" and "never" not in actions[1]["detail"]
        results = [await world.toolbox.execute(a) for a in actions]
        assert all(r["ok"] for r in results), results
        assert results[0]["transaction_id"] == "tx1"
        assert world.fake.stock[1] == 4
        assert results[1]["created_product_id"] == 4 and world.fake.stock[4] == 1
        assert world.fake.products[4]["qu_id_consume"] == 3

    run(go())


def test_unknown_product_and_duplicate_new_product_are_rejected(world):
    async def go():
        with pytest.raises(ToolError, match="no product with id 99"):
            await world.toolbox.plan("consume_products", {"items": [{"product_id": 99, "amount": 1}]})
        with pytest.raises(ToolError, match="already exists with id 1"):
            await world.toolbox.plan("add_purchases", {"items": [{"new_product": {"name": "milk", "location_id": 1, "qu_id": 2}, "amount": 1}]})
        with pytest.raises(ToolError, match="not in stock"):
            await world.toolbox.plan("consume_products", {"items": [{"product_id": 3, "all": True}]})

    run(go())


def test_consume_all_and_clamping(world):
    async def go():
        actions = await world.toolbox.plan("consume_products", {"items": [{"product_id": 1, "all": True}, {"product_id": 2, "amount": 10}]})
        assert actions[0]["detail"] == "Use all: 2 bottles"
        assert actions[1]["args"]["amount"] == 6 and "only 6 pieces in stock" in actions[1]["detail"]
        result = await world.toolbox.execute(actions[0])
        assert result["ok"] and "0 bottles left" in result["summary"]

    run(go())


def test_search_handles_typos(world):
    async def go():
        found = await world.toolbox.read("search_products", {"query": "fenel seed"})
        assert found[0]["name"] == "Fennel seeds"

    run(go())


# ---------------------------------------------------------------- agent loop

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


class ScriptedClient:
    """Stands in for AsyncAnthropic: returns the scripted responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def stream(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        content = self.responses.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        message = SimpleNamespace(content=content, stop_reason=stop, usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def __aiter__(self):
                async def events():
                    for b in content:
                        if b.type == "text":
                            yield SimpleNamespace(type="text", text=b.text)
                return events()

            async def get_final_message(self):
                return message

        return Stream()


def make(world, responses):
    client = ScriptedClient(responses)
    agent = Agent(client, world.db, world.toolbox)
    run(world.catalog.refresh())
    conv = world.db.create("mom@example.com", world.catalog.text())
    events = []

    async def emit(e):
        events.append(e)

    return client, agent, conv, events, emit


def user(t):
    return [{"type": "text", "text": t}]


def test_gated_write_waits_for_confirmation_then_runs(world):
    client, agent, conv, events, emit = make(world, [
        [tool("t1", "consume_products", {"items": [{"product_id": 2, "amount": 2}, {"product_id": 1, "amount": 1}]}),
         tool("t2", "add_to_shopping_list", {"items": [{"product_id": 3}]})],
        [text("Done: 2 eggs off. Fennel seeds are on the list.")],
    ])
    run(agent.user_turn(conv, user("used 2 eggs and some milk, need saunf"), emit))

    # Nothing was consumed yet; the shopping list change ran straight away.
    assert world.fake.stock == {1: 2.0, 2: 6.0}
    assert world.fake.shopping[1]["product_id"] == 3
    card = next(e["item"] for e in events if e["type"] == "item" and e["item"].get("state") == "pending")
    assert [a["title"] for a in card["actions"]] == ["Eggs", "Milk"]
    assert conv.pending and conv.messages[-1]["role"] == "assistant"

    # Confirm only the eggs line.
    conv = world.db.get(conv.id, "mom@example.com")
    run(agent.confirm(conv, {"0:0"}, emit))
    assert world.fake.stock == {1: 2.0, 2: 4.0}
    sent = client.requests[-1][-1]["content"]
    assert [b["tool_use_id"] for b in sent] == ["t1", "t2"]
    rows = json.loads(sent[0]["content"])["results"]
    assert rows[0]["status"] == "done" and rows[0]["transaction_id"] == "tx1"
    assert rows[1]["status"].startswith("skipped")
    done = world.db.shown(conv.id, card["id"])[0]
    assert done["state"] == "done" and done["actions"][1]["skipped"]
    assert world.db.get(conv.id, "mom@example.com").pending is None
    assert world.db.turns_today("mom@example.com") == 1


def test_cancel_changes_nothing(world):
    client, agent, conv, events, emit = make(world, [
        [tool("t1", "consume_products", {"items": [{"product_id": 1, "all": True}]})],
        [text("Okay, nothing changed.")],
    ])
    run(agent.user_turn(conv, user("we're out of milk"), emit))
    run(agent.confirm(conv, set(), emit))
    assert world.fake.stock[1] == 2.0
    assert "Cancel" in client.requests[-1][-1]["content"][0]["content"]


def test_typing_instead_of_confirming_settles_the_pending_call(world):
    client, agent, conv, events, emit = make(world, [
        [tool("t1", "consume_products", {"items": [{"product_id": 1, "amount": 1}]})],
        [text("Which one did you mean?")],
    ])
    run(agent.user_turn(conv, user("used milk"), emit))
    run(agent.user_turn(conv, user("no, the eggs"), emit))
    last = client.requests[-1][-1]["content"]
    assert last[0]["type"] == "tool_result" and "did not confirm" in last[0]["content"]
    assert last[1] == {"type": "text", "text": "no, the eggs"}
    assert world.fake.stock[1] == 2.0
    # History only ever grows: each request starts with the previous one.
    assert client.requests[1][: len(client.requests[0])] == client.requests[0]


def test_bad_tool_input_goes_back_to_the_model_without_a_card(world):
    client, agent, conv, events, emit = make(world, [
        [tool("t1", "consume_products", {"items": [{"product_id": 42, "amount": 1}]})],
        [text("I couldn't find that.")],
    ])
    run(agent.user_turn(conv, user("used the paneer"), emit))
    result = client.requests[-1][-1]["content"][0]
    assert result["is_error"] and "no product with id 42" in result["content"]
    assert not any(e["type"] == "item" and e["item"]["t"] == "card" for e in events)


def test_ask_user_pauses_and_gated_calls_beside_it_are_refused(world):
    client, agent, conv, events, emit = make(world, [
        [tool("t1", "ask_user", {"question": "Which milk?", "options": ["Whole", "Almond"]}),
         tool("t2", "consume_products", {"items": [{"product_id": 1, "amount": 1}]})],
        [text("Got it.")],
    ])
    run(agent.user_turn(conv, user("used milk"), emit))
    assert conv.pending["ask"] and not conv.pending["gated"]
    assert any(e["type"] == "item" and e["item"]["t"] == "ask" for e in events)
    run(agent.user_turn(conv, user("Whole"), emit))
    sent = client.requests[-1][-1]["content"]
    assert [b.get("tool_use_id") for b in sent[:2]] == ["t1", "t2"] and sent[1]["is_error"]
    assert world.fake.stock[1] == 2.0
