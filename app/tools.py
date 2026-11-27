"""The tools Claude gets: thin wrappers over Grocy's REST API.

Read tools run straight away. Every write tool is first turned into a list of
small "actions" (plan), each with a human-readable title the user sees. Gated
tools wait for the user to confirm those actions in the UI; the others run
immediately. Either way the same `execute` runs one action against Grocy.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .grocy import Catalog, Grocy, GrocyError, fmt_num, to_float, to_int

NEVER = "2999-12-31"


class ToolError(Exception):
    """Bad input from the model; the message goes back to it as an error result."""


# ---------------------------------------------------------------- schemas

def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def arr(items: dict, desc: str = "") -> dict:
    return {"type": "array", "items": items, "description": desc, "minItems": 1}


INT = {"type": "integer"}
NUM = {"type": "number"}
STR = {"type": "string"}
BOOL = {"type": "boolean"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}


def d(schema: dict, description: str) -> dict:
    return {**schema, "description": description}


NEW_PRODUCT = obj(
    {
        "name": d(STR, "Clean generic name in the style of existing products, e.g. 'Fennel seeds'."),
        "location_id": d(INT, "Where it is normally kept."),
        "qu_id": d(INT, "Quantity unit id used for stock, purchase and consumption."),
        "product_group_id": INT,
        "default_best_before_days": d(INT, "Typical shelf life in days; -1 if it never expires."),
        "min_stock_amount": d(NUM, "Below this amount Grocy treats it as missing. Default 0."),
    },
    ["name", "location_id", "qu_id"],
)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    label: str  # shown in the chat while the tool runs
    kind: str = "read"  # read | auto (write, runs immediately) | gated (write, needs a tap) | ask
    feature: str = ""  # Grocy feature flag that must be on

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


TOOLS: list[Tool] = [
    Tool(
        "search_products",
        "Fuzzy-search products by name. Returns the closest matches with current stock. "
        "The catalog in the system prompt is usually enough; use this when it may be stale or truncated.",
        obj({"query": STR}, ["query"]),
        "Looking up products",
    ),
    Tool(
        "get_stock",
        "Current stock. With product_id: that product's amount, opened amount, next due date and each "
        "stock entry (with entry ids, locations, prices). Without: every product currently in stock, "
        "optionally limited to one location.",
        obj({"product_id": INT, "location_id": INT}),
        "Checking stock",
    ),
    Tool(
        "get_product_details",
        "Master data of one product: units, unit conversions, default location, shelf life, minimum "
        "stock, barcodes, last and average price. Use before converting between units.",
        obj({"product_id": INT}, ["product_id"]),
        "Checking product details",
    ),
    Tool(
        "get_expiring",
        "Products that are due soon, overdue, expired, or below their minimum stock.",
        obj({"days": d(INT, "How many days ahead counts as 'soon'. Default 5.")}),
        "Checking what is expiring",
    ),
    Tool(
        "get_stock_log",
        "Recent stock bookings (purchases, consumption, corrections), newest first, with the "
        "transaction ids needed for undo_transactions.",
        obj({"product_id": INT, "limit": d(INT, "Default 15, max 50.")}),
        "Looking at recent changes",
    ),
    Tool(
        "get_shopping_list",
        "Items on a shopping list with their item ids, amounts, notes and done state.",
        obj({"list_id": d(INT, "Default 1.")}),
        "Reading the shopping list",
    ),
    Tool(
        "get_master_data",
        "Fresh list of locations, quantity units, stores, product groups or shopping lists.",
        obj({"kind": {"type": "string", "enum": ["locations", "quantity_units", "stores", "product_groups", "shopping_lists"]}}, ["kind"]),
        "Checking Grocy settings",
    ),
    Tool(
        "add_purchases",
        "Add bought items to stock. One call for a whole receipt or shopping trip. Each item is either "
        "an existing product (product_id) or a product that does not exist yet (new_product), which is "
        "created first. Amounts are in the product's stock unit. Bought items are also taken off the "
        "shopping list. The user confirms the items on a card before anything is saved.",
        obj(
            {
                "items": arr(
                    obj(
                        {
                            "product_id": INT,
                            "new_product": NEW_PRODUCT,
                            "amount": d(NUM, "In the stock unit."),
                            "total_price": d(NUM, "Price paid for this line in total, not per unit."),
                            "best_before_date": d(DATE, "Omit to use the product's default shelf life."),
                            "location_id": d(INT, "Omit for the product's default location."),
                            "note": STR,
                        },
                        ["amount"],
                    )
                ),
                "store_id": INT,
                "purchased_date": d(DATE, "Omit for today."),
            },
            ["items"],
        ),
        "Preparing purchases",
        "gated",
    ),
    Tool(
        "consume_products",
        "Take used-up amounts out of stock. Set all=true for 'we are out of X' or 'used the last X'. "
        "Set spoiled=true for things thrown away. The user confirms on a card.",
        obj(
            {
                "items": arr(
                    obj(
                        {
                            "product_id": INT,
                            "amount": d(NUM, "In the stock unit. Not needed with all=true."),
                            "all": BOOL,
                            "spoiled": BOOL,
                            "location_id": d(INT, "Only take from this location."),
                        },
                        ["product_id"],
                    )
                )
            },
            ["items"],
        ),
        "Preparing stock update",
        "gated",
    ),
    Tool(
        "open_products",
        "Mark an amount of products as opened (Grocy then applies the shorter after-opening shelf life). "
        "The user confirms on a card.",
        obj({"items": arr(obj({"product_id": INT, "amount": d(NUM, "Default 1.")}, ["product_id"]))}, ["items"]),
        "Preparing stock update",
        "gated",
    ),
    Tool(
        "set_stock_amounts",
        "Inventory correction: set the true amount in stock when Grocy's number is wrong "
        "('we actually have 3 cans'). The user confirms on a card.",
        obj(
            {
                "items": arr(
                    obj(
                        {
                            "product_id": INT,
                            "new_amount": NUM,
                            "best_before_date": d(DATE, "For any amount that gets added."),
                            "location_id": INT,
                        },
                        ["product_id", "new_amount"],
                    )
                )
            },
            ["items"],
        ),
        "Preparing stock correction",
        "gated",
    ),
    Tool(
        "transfer_stock",
        "Move an amount of a product from one location to another (e.g. freezer to fridge). The user confirms.",
        obj(
            {"product_id": INT, "amount": NUM, "from_location_id": INT, "to_location_id": INT},
            ["product_id", "amount", "from_location_id", "to_location_id"],
        ),
        "Preparing move",
        "gated",
    ),
    Tool(
        "edit_stock_entry",
        "Change one existing stock entry (ids from get_stock with product_id): its amount, best before "
        "date, price per unit, location or opened state. The user confirms.",
        obj(
            {"entry_id": INT, "amount": NUM, "best_before_date": DATE, "price": NUM, "location_id": INT, "open": BOOL},
            ["entry_id"],
        ),
        "Preparing change",
        "gated",
    ),
    Tool(
        "undo_transactions",
        "Reverse earlier stock bookings by transaction id (from an earlier tool result or get_stock_log). "
        "The user confirms.",
        obj({"transaction_ids": arr(STR)}, ["transaction_ids"]),
        "Preparing undo",
        "gated",
    ),
    Tool(
        "add_to_shopping_list",
        "Add items to a shopping list. Use product_id for known products, or note alone for free-text "
        "items that are not products. Runs immediately, no confirmation.",
        obj(
            {
                "items": arr(obj({"product_id": INT, "amount": d(NUM, "Default 1."), "note": STR})),
                "list_id": d(INT, "Default 1."),
            },
            ["items"],
        ),
        "Updating the shopping list",
        "auto",
    ),
    Tool(
        "remove_from_shopping_list",
        "Remove items from the shopping list by item id (from get_shopping_list). Runs immediately.",
        obj({"item_ids": arr(INT)}, ["item_ids"]),
        "Updating the shopping list",
        "auto",
    ),
    Tool(
        "update_shopping_list_item",
        "Change the amount or note of a shopping list item, or tick it off / untick it. Runs immediately.",
        obj({"item_id": INT, "amount": NUM, "note": STR, "done": BOOL}, ["item_id"]),
        "Updating the shopping list",
        "auto",
    ),
    Tool(
        "add_low_stock_to_shopping_list",
        "Put every product that is below its minimum stock on the shopping list. Runs immediately.",
        obj({"list_id": d(INT, "Default 1.")}),
        "Updating the shopping list",
        "auto",
    ),
    Tool(
        "clear_shopping_list",
        "Remove all items (or with done_only just the ticked ones) from a shopping list. The user confirms.",
        obj({"list_id": d(INT, "Default 1."), "done_only": BOOL}),
        "Preparing to clear the list",
        "gated",
    ),
    Tool(
        "create_product",
        "Create a product without adding stock (for stock use add_purchases with new_product). The user confirms.",
        NEW_PRODUCT,
        "Preparing new product",
        "gated",
    ),
    Tool(
        "update_product",
        "Edit a product's master data. Only the given fields change. The user confirms.",
        obj(
            {
                "product_id": INT,
                "name": STR,
                "description": STR,
                "location_id": INT,
                "product_group_id": INT,
                "min_stock_amount": NUM,
                "default_best_before_days": INT,
                "default_best_before_days_after_open": INT,
                "store_id": d(INT, "Default store."),
            },
            ["product_id"],
        ),
        "Preparing product change",
        "gated",
    ),
    Tool(
        "create_master_item",
        "Create a location, quantity unit, store or product group. The user confirms.",
        obj(
            {
                "kind": {"type": "string", "enum": ["location", "quantity_unit", "store", "product_group"]},
                "name": STR,
                "name_plural": d(STR, "Quantity units only."),
                "is_freezer": d(BOOL, "Locations only."),
            },
            ["kind", "name"],
        ),
        "Preparing new entry",
        "gated",
    ),
    Tool(
        "get_recipes",
        "Recipes saved in Grocy with whether current stock covers them. With recipe_id: the ingredients and instructions.",
        obj({"recipe_id": INT}),
        "Looking at recipes",
        feature="FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "consume_recipe",
        "Take all ingredients of a saved recipe out of stock (it was cooked). The user confirms.",
        obj({"recipe_id": INT}, ["recipe_id"]),
        "Preparing stock update",
        "gated",
        "FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "add_recipe_missing_to_shopping_list",
        "Put the ingredients a saved recipe is missing on the shopping list. Runs immediately.",
        obj({"recipe_id": INT}, ["recipe_id"]),
        "Updating the shopping list",
        "auto",
        "FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "create_recipe",
        "Save a new recipe with ingredients linked to products. The user confirms.",
        obj(
            {
                "name": STR,
                "instructions": d(STR, "Plain text, one step per line."),
                "servings": d(INT, "Default 1."),
                "ingredients": arr(obj({"product_id": INT, "amount": NUM, "note": STR}, ["product_id", "amount"])),
            },
            ["name", "ingredients"],
        ),
        "Preparing recipe",
        "gated",
        "FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "get_meal_plan",
        "Meal plan entries between two dates (default: today and the next 7 days).",
        obj({"from_date": DATE, "to_date": DATE}),
        "Looking at the meal plan",
        feature="FEATURE_FLAG_RECIPES_MEALPLAN",
    ),
    Tool(
        "add_meal_plan_entry",
        "Plan a meal for a day: a saved recipe, a product, or a free-text note. Runs immediately.",
        obj({"day": DATE, "recipe_id": INT, "product_id": INT, "note": STR, "servings": d(NUM, "Default 1.")}, ["day"]),
        "Updating the meal plan",
        "auto",
        "FEATURE_FLAG_RECIPES_MEALPLAN",
    ),
    Tool(
        "get_chores",
        "Household chores with when each is next due.",
        obj({}),
        "Looking at chores",
        feature="FEATURE_FLAG_CHORES",
    ),
    Tool(
        "track_chore",
        "Record that a chore was done just now. Runs immediately.",
        obj({"chore_id": INT}, ["chore_id"]),
        "Recording chore",
        "auto",
        "FEATURE_FLAG_CHORES",
    ),
    Tool("get_tasks", "Open to-do tasks.", obj({}), "Looking at tasks", feature="FEATURE_FLAG_TASKS"),
    Tool(
        "add_task",
        "Add a to-do task. Runs immediately.",
        obj({"name": STR, "due_date": DATE, "description": STR}, ["name"]),
        "Adding task",
        "auto",
        "FEATURE_FLAG_TASKS",
    ),
    Tool(
        "complete_task",
        "Mark a task as done. Runs immediately.",
        obj({"task_id": INT}, ["task_id"]),
        "Completing task",
        "auto",
        "FEATURE_FLAG_TASKS",
    ),
    Tool(
        "ask_user",
        "Ask the user one short question with 2 to 5 tappable answers, e.g. to pick between similar "
        "products or to offer adding something to the shopping list. They can also answer in their own "
        "words. Your turn ends when you call this, so call it alone and write nothing after it.",
        obj({"question": STR, "options": {"type": "array", "items": STR, "minItems": 2, "maxItems": 5}}, ["question", "options"]),
        "Asking",
        "ask",
    ),
]

BY_NAME = {t.name: t for t in TOOLS}


def strip_html(text: str | None) -> str:
    return re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_date(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ToolError(f"{field} must be a date like 2026-01-31, got {value!r}")


def nice_date(iso: str | None) -> str:
    if not iso:
        return ""
    if iso.startswith("2999"):
        return "never expires"
    try:
        dt = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return dt.strftime("%b %-d") if dt.year == date.today().year else dt.strftime("%b %-d, %Y")


def action(op: str, kind: str, title: str, detail: str, **args: Any) -> dict:
    return {"op": op, "kind": kind, "title": title, "detail": detail, "args": args}


class Toolbox:
    def __init__(self, grocy: Grocy, catalog: Catalog, today=date.today):
        self.g = grocy
        self.cat = catalog
        self.today = today

    def enabled(self) -> list[Tool]:
        return [t for t in TOOLS if not t.feature or self.cat.feature(t.feature)]

    # ------------------------------------------------------------ helpers

    async def need_product(self, product_id: Any) -> dict:
        p = self.cat.product(product_id)
        if not p:
            await self.cat.refresh(force=True)
            p = self.cat.product(product_id)
        if not p:
            raise ToolError(f"There is no product with id {product_id}. Use an id from the catalog or search_products.")
        return p

    def need_location(self, location_id: Any) -> int:
        lid = to_int(location_id)
        if lid not in self.cat.locations:
            raise ToolError(f"There is no location with id {location_id}.")
        return lid

    def need_amount(self, value: Any, field: str = "amount") -> float:
        amount = to_float(value)
        if amount <= 0:
            raise ToolError(f"{field} must be greater than 0.")
        return amount

    async def stock_amount(self, product_id: int) -> float:
        details = await self.g.get(f"/stock/products/{product_id}")
        return to_float(details.get("stock_amount"))

    def check_new_product(self, np: dict) -> dict:
        name = str(np.get("name", "")).strip()
        if not name:
            raise ToolError("new_product needs a name.")
        clash = next((p for p in self.cat.products.values() if p["name"].strip().lower() == name.lower()), None)
        if clash:
            raise ToolError(f"A product named {clash['name']!r} already exists with id {clash['id']}. Use that product_id.")
        qu = to_int(np.get("qu_id"))
        if qu not in self.cat.units:
            raise ToolError(f"There is no quantity unit with id {np.get('qu_id')}.")
        fields = {
            "name": name,
            "location_id": self.need_location(np.get("location_id")),
            "qu_id_stock": qu,
            "qu_id_purchase": qu,
            "qu_id_consume": qu,
            "qu_id_price": qu,
            "min_stock_amount": to_float(np.get("min_stock_amount")),
            "default_best_before_days": to_int(np.get("default_best_before_days")) or 0,
        }
        group = to_int(np.get("product_group_id"))
        if group:
            if group not in self.cat.groups:
                raise ToolError(f"There is no product group with id {group}.")
            fields["product_group_id"] = group
        return fields

    def default_best_before(self, days: Any) -> str:
        days = to_int(days) or 0
        return (self.today() + timedelta(days=days)).isoformat() if days > 0 else NEVER

    # --------------------------------------------------------- read tools

    async def read(self, name: str, inp: dict) -> Any:
        await self.cat.refresh()
        return await getattr(self, f"r_{name}")(inp)

    async def r_search_products(self, inp: dict) -> Any:
        query = str(inp.get("query", "")).lower().strip()
        if not query:
            raise ToolError("query is empty.")
        await self.cat.refresh(force=True)
        words = query.split()
        scored = []
        for p in self.cat.products.values():
            name = p["name"].lower()
            score = difflib.SequenceMatcher(None, query, name).ratio()
            if query in name:
                score = max(score, 0.9)
            elif words and all(any(w in part or difflib.SequenceMatcher(None, w, part).ratio() > 0.8 for part in name.split()) for w in words):
                score = max(score, 0.8)
            if score >= 0.5:
                scored.append((score, p))
        scored.sort(key=lambda s: -s[0])
        stock = {to_int(s["product_id"]): s for s in await self.g.get("/stock")}
        return [
            {
                "product_id": p["id"],
                "name": p["name"],
                "in_stock": self.cat.amount(p["id"], stock.get(int(p["id"]), {}).get("amount", 0)),
                "location": self.cat.location_name(p.get("location_id")),
            }
            for _, p in scored[:8]
        ] or "No product matches. It may not exist yet."

    async def r_get_stock(self, inp: dict) -> Any:
        pid = to_int(inp.get("product_id"))
        if pid:
            await self.need_product(pid)
            details = await self.g.get(f"/stock/products/{pid}")
            entries = await self.g.get(f"/stock/products/{pid}/entries")
            return {
                "product_id": pid,
                "name": self.cat.product_name(pid),
                "in_stock": self.cat.amount(pid, details.get("stock_amount")),
                "opened": self.cat.amount(pid, details.get("stock_amount_opened")),
                "next_due": details.get("next_due_date"),
                "min_stock": fmt_num(details["product"].get("min_stock_amount")),
                "entries": [
                    {
                        "entry_id": e["id"],
                        "amount": fmt_num(e["amount"]),
                        "best_before": e.get("best_before_date"),
                        "location": self.cat.location_name(e.get("location_id")),
                        "opened": bool(to_int(e.get("open"))),
                        "purchased": e.get("purchased_date"),
                        "price_per_unit": e.get("price"),
                    }
                    for e in entries
                ],
            }
        rows = await self.g.get("/stock")
        loc = to_int(inp.get("location_id"))
        if loc:
            self.need_location(loc)
            here: dict[int, float] = {}
            for e in await self.g.get(f"/stock/locations/{loc}/entries"):
                here[int(e["product_id"])] = here.get(int(e["product_id"]), 0) + to_float(e["amount"])
            rows = [{**r, "amount": here[int(r["product_id"])]} for r in rows if int(r["product_id"]) in here]
        out = []
        for r in sorted(rows, key=lambda r: self.cat.product_name(r["product_id"]).lower()):
            row = {"product_id": to_int(r["product_id"]), "name": self.cat.product_name(r["product_id"]), "amount": self.cat.amount(r["product_id"], r["amount"])}
            if to_float(r.get("amount_opened")):
                row["opened"] = fmt_num(r["amount_opened"])
            if r.get("best_before_date") and not str(r["best_before_date"]).startswith("2999"):
                row["next_due"] = r["best_before_date"]
            out.append(row)
        return out or "Nothing is in stock."

    async def r_get_product_details(self, inp: dict) -> Any:
        pid = to_int(inp.get("product_id"))
        p = await self.need_product(pid)
        details = await self.g.get(f"/stock/products/{pid}")
        conversions = await self.g.objects("quantity_unit_conversions_resolved", [f"product_id={pid}"])
        return {
            "product_id": pid,
            "name": p["name"],
            "description": strip_html(p.get("description")),
            "stock_unit": self.cat.unit_name(p.get("qu_id_stock")),
            "purchase_unit": self.cat.unit_name(p.get("qu_id_purchase")),
            "unit_conversions": [
                f"1 {self.cat.unit_name(c['from_qu_id'])} = {fmt_num(c['factor'])} {self.cat.unit_name(c['to_qu_id'], c['factor'])}"
                for c in conversions
                if to_int(c["from_qu_id"]) != to_int(c["to_qu_id"]) and to_int(c["to_qu_id"]) == to_int(p.get("qu_id_stock"))
            ],
            "default_location": self.cat.location_name(p.get("location_id")),
            "default_best_before_days": p.get("default_best_before_days"),
            "default_best_before_days_after_open": p.get("default_best_before_days_after_open"),
            "min_stock_amount": fmt_num(p.get("min_stock_amount")),
            "in_stock": self.cat.amount(pid, details.get("stock_amount")),
            "last_purchased": details.get("last_purchased"),
            "last_price_per_unit": details.get("last_price"),
            "average_price_per_unit": details.get("avg_price"),
            "barcodes": [b["barcode"] for b in details.get("product_barcodes") or []],
        }

    async def r_get_expiring(self, inp: dict) -> Any:
        days = to_int(inp.get("days")) or 5
        v = await self.g.get("/stock/volatile", due_soon_days=days)

        def rows(key: str) -> list[dict]:
            return [
                {
                    "product_id": to_int(r["product_id"]),
                    "name": self.cat.product_name(r["product_id"]),
                    "amount": self.cat.amount(r["product_id"], r["amount"]),
                    "best_before": r.get("best_before_date"),
                }
                for r in v.get(key) or []
            ]

        return {
            "today": self.today().isoformat(),
            f"due_within_{days}_days": rows("due_products"),
            "overdue_but_usually_still_fine": rows("overdue_products"),
            "expired": rows("expired_products"),
            "below_minimum_stock": [
                {"product_id": to_int(m["id"]), "name": m["name"], "missing": self.cat.amount(m["id"], m["amount_missing"])}
                for m in v.get("missing_products") or []
            ],
        }

    async def r_get_stock_log(self, inp: dict) -> Any:
        limit = min(to_int(inp.get("limit")) or 15, 50)
        pid = to_int(inp.get("product_id"))
        rows = await self.g.objects("stock_log", [f"product_id={pid}"] if pid else None, order="id:desc", limit=limit)
        return [
            {
                "transaction_id": r.get("transaction_id"),
                "type": r.get("transaction_type"),
                "product": self.cat.product_name(r["product_id"]),
                "amount": fmt_num(r["amount"]),
                "when": r.get("row_created_timestamp"),
                "already_undone": bool(to_int(r.get("undone"))),
            }
            for r in rows
        ] or "No bookings yet."

    async def r_get_shopping_list(self, inp: dict) -> Any:
        list_id = to_int(inp.get("list_id")) or 1
        rows = await self.g.objects("shopping_list", [f"shopping_list_id={list_id}"])
        out = []
        for r in rows:
            pid = to_int(r.get("product_id"))
            out.append(
                {
                    "item_id": to_int(r["id"]),
                    "product_id": pid,
                    "name": self.cat.product_name(pid) if pid else (r.get("note") or "(empty)"),
                    "amount": self.cat.amount(pid, r["amount"]) if pid else fmt_num(r["amount"]),
                    "note": r.get("note") if pid else None,
                    "done": bool(to_int(r.get("done"))),
                }
            )
        return out or "The shopping list is empty."

    async def r_get_master_data(self, inp: dict) -> Any:
        await self.cat.refresh(force=True)
        source = {
            "locations": self.cat.locations,
            "quantity_units": self.cat.units,
            "stores": self.cat.stores,
            "product_groups": self.cat.groups,
            "shopping_lists": self.cat.lists,
        }.get(str(inp.get("kind")))
        if source is None:
            raise ToolError("Unknown kind.")
        return [{"id": i, "name": r["name"]} for i, r in sorted(source.items())] or "None yet."

    async def r_get_recipes(self, inp: dict) -> Any:
        rid = to_int(inp.get("recipe_id"))
        if rid:
            recipe = await self.g.get(f"/objects/recipes/{rid}")
            positions = await self.g.objects("recipes_pos", [f"recipe_id={rid}"])
            return {
                "recipe_id": rid,
                "name": recipe["name"],
                "servings": recipe.get("base_servings"),
                "instructions": strip_html(recipe.get("description")),
                "ingredients": [
                    {"product_id": to_int(p["product_id"]), "name": self.cat.product_name(p["product_id"]), "amount": fmt_num(p["amount"]), "unit": self.cat.unit_name(p.get("qu_id"), p["amount"]), "note": p.get("note") or None}
                    for p in positions
                ],
            }
        recipes = await self.g.objects("recipes", ["type=normal"], order="name")
        fulfil = {to_int(f["recipe_id"]): f for f in await self.g.get("/recipes/fulfillment")}
        return [
            {
                "recipe_id": to_int(r["id"]),
                "name": r["name"],
                "can_cook_now": bool(to_int(fulfil.get(int(r["id"]), {}).get("need_fulfilled"))),
                "missing_ingredients": to_int(fulfil.get(int(r["id"]), {}).get("missing_products_count")) or 0,
            }
            for r in recipes
        ] or "No recipes are saved in Grocy."

    async def r_get_meal_plan(self, inp: dict) -> Any:
        start = parse_date(inp.get("from_date"), "from_date") or self.today().isoformat()
        end = parse_date(inp.get("to_date"), "to_date") or (self.today() + timedelta(days=7)).isoformat()
        rows = await self.g.objects("meal_plan", [f"day>={start}", f"day<={end}"], order="day")
        out = []
        for r in rows:
            what = r.get("note") or ""
            if r.get("type") == "recipe" and r.get("recipe_id"):
                what = (await self.g.get(f"/objects/recipes/{r['recipe_id']}"))["name"]
            elif r.get("type") == "product":
                what = self.cat.product_name(r.get("product_id"))
            out.append({"day": r["day"], "type": r.get("type"), "what": what})
        return out or "Nothing is planned in that period."

    async def r_get_chores(self, inp: dict) -> Any:
        return [
            {"chore_id": to_int(c["chore_id"]), "name": c["chore_name"], "next_due": c.get("next_estimated_execution_time"), "last_done": c.get("last_tracked_time")}
            for c in await self.g.get("/chores")
        ] or "No chores are set up."

    async def r_get_tasks(self, inp: dict) -> Any:
        return [
            {"task_id": to_int(t["id"]), "name": t["name"], "due_date": t.get("due_date") or None}
            for t in await self.g.get("/tasks")
        ] or "No open tasks."

    # --------------------------------------------------- write tools: plan

    async def plan(self, name: str, inp: dict) -> list[dict]:
        """Validate a write tool's input and split it into user-readable actions."""
        await self.cat.refresh()
        actions = await getattr(self, f"p_{name}")(inp)
        if not actions:
            raise ToolError("Nothing to do: no items were given.")
        return actions

    def items(self, inp: dict) -> list[dict]:
        items = inp.get("items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ToolError("items must be a list of objects.")
        return items

    async def p_add_purchases(self, inp: dict) -> list[dict]:
        store = to_int(inp.get("store_id"))
        if store and store not in self.cat.stores:
            raise ToolError(f"There is no store with id {store}.")
        purchased = parse_date(inp.get("purchased_date"), "purchased_date")
        out = []
        for it in self.items(inp):
            amount = self.need_amount(it.get("amount"))
            best = parse_date(it.get("best_before_date"), "best_before_date")
            loc = self.need_location(it["location_id"]) if it.get("location_id") else None
            total = to_float(it.get("total_price")) if it.get("total_price") is not None else None
            new = None
            if it.get("product_id"):
                p = await self.need_product(it["product_id"])
                pid, name = int(p["id"]), p["name"]
                unit = self.cat.unit_name(p.get("qu_id_stock"), amount)
                best = best or self.default_best_before(p.get("default_best_before_days"))
                where = self.cat.location_name(loc or p.get("location_id"))
            elif isinstance(it.get("new_product"), dict):
                new = self.check_new_product(it["new_product"])
                pid, name = None, new["name"]
                unit = self.cat.unit_name(new["qu_id_stock"], amount)
                best = best or self.default_best_before(new["default_best_before_days"])
                where = self.cat.location_name(loc or new["location_id"])
            else:
                raise ToolError("Each item needs product_id or new_product.")
            parts = [f"+{fmt_num(amount)} {unit}".strip()]
            if total:
                parts.append(self.cat.money(total))
            if not best.startswith("2999"):
                parts.append(f"best before {nice_date(best)}")
            if where:
                parts.append(where)
            out.append(
                action(
                    "purchase", "new" if new else "add", name, " · ".join(parts),
                    product_id=pid, new_product=new, amount=amount,
                    price=round(total / amount, 4) if total else None,
                    best_before_date=best, location_id=loc, store_id=store,
                    purchased_date=purchased, note=it.get("note") or None,
                )
            )
        return out

    async def p_consume_products(self, inp: dict) -> list[dict]:
        out = []
        for it in self.items(inp):
            p = await self.need_product(it.get("product_id"))
            pid = int(p["id"])
            in_stock = await self.stock_amount(pid)
            if in_stock <= 0:
                raise ToolError(f"{p['name']} is not in stock according to Grocy, so nothing can be taken out. Tell the user, or leave it out.")
            everything = bool(it.get("all"))
            amount = in_stock if everything else self.need_amount(it.get("amount"))
            note = ""
            if amount > in_stock:
                amount, note, everything = in_stock, f" (only {self.cat.amount(pid, in_stock)} in stock)", True
            spoiled = bool(it.get("spoiled"))
            verb = "Throw away" if spoiled else "Use"
            detail = f"{verb} all: {self.cat.amount(pid, amount)}{note}" if everything else f"{verb} {self.cat.amount(pid, amount)} of {fmt_num(in_stock)}"
            loc = self.need_location(it["location_id"]) if it.get("location_id") else None
            out.append(action("consume", "remove", p["name"], detail, product_id=pid, amount=amount, spoiled=spoiled, location_id=loc))
        return out

    async def p_open_products(self, inp: dict) -> list[dict]:
        out = []
        for it in self.items(inp):
            p = await self.need_product(it.get("product_id"))
            amount = self.need_amount(it.get("amount", 1))
            if await self.stock_amount(int(p["id"])) <= 0:
                raise ToolError(f"{p['name']} is not in stock according to Grocy, so it cannot be marked as opened.")
            out.append(action("open", "open", p["name"], f"Mark {self.cat.amount(p['id'], amount)} as opened", product_id=int(p["id"]), amount=amount))
        return out

    async def p_set_stock_amounts(self, inp: dict) -> list[dict]:
        out = []
        for it in self.items(inp):
            p = await self.need_product(it.get("product_id"))
            pid = int(p["id"])
            new_amount = to_float(it.get("new_amount"))
            if new_amount < 0:
                raise ToolError("new_amount cannot be negative.")
            current = await self.stock_amount(pid)
            if new_amount == current:
                raise ToolError(f"{p['name']} is already at {self.cat.amount(pid, current)}; nothing to correct.")
            best = parse_date(it.get("best_before_date"), "best_before_date") or self.default_best_before(p.get("default_best_before_days"))
            loc = self.need_location(it["location_id"]) if it.get("location_id") else None
            out.append(
                action("inventory", "edit", p["name"], f"Set stock to {self.cat.amount(pid, new_amount)} (now {fmt_num(current)})",
                       product_id=pid, new_amount=new_amount, best_before_date=best, location_id=loc)
            )
        return out

    async def p_transfer_stock(self, inp: dict) -> list[dict]:
        p = await self.need_product(inp.get("product_id"))
        amount = self.need_amount(inp.get("amount"))
        src, dst = self.need_location(inp.get("from_location_id")), self.need_location(inp.get("to_location_id"))
        return [
            action("transfer", "edit", p["name"], f"Move {self.cat.amount(p['id'], amount)} from {self.cat.location_name(src)} to {self.cat.location_name(dst)}",
                   product_id=int(p["id"]), amount=amount, location_id_from=src, location_id_to=dst)
        ]

    async def p_edit_stock_entry(self, inp: dict) -> list[dict]:
        entry_id = to_int(inp.get("entry_id"))
        entry = await self.g.get(f"/stock/entry/{entry_id}")
        if not entry:
            raise ToolError(f"There is no stock entry with id {entry_id}.")
        pid = entry["product_id"]
        body = {
            "amount": to_float(entry["amount"]),
            "best_before_date": entry.get("best_before_date"),
            "price": entry.get("price"),
            "open": bool(to_int(entry.get("open"))),
            "location_id": to_int(entry.get("location_id")),
            "shopping_location_id": to_int(entry.get("shopping_location_id")),
            "purchased_date": entry.get("purchased_date"),
        }
        changes = []
        if inp.get("amount") is not None:
            body["amount"] = self.need_amount(inp["amount"])
            changes.append(f"amount {self.cat.amount(pid, body['amount'])}")
        if inp.get("best_before_date"):
            body["best_before_date"] = parse_date(inp["best_before_date"], "best_before_date")
            changes.append(f"best before {nice_date(body['best_before_date'])}")
        if inp.get("price") is not None:
            body["price"] = to_float(inp["price"])
            changes.append(f"price {self.cat.money(body['price'])}")
        if inp.get("location_id"):
            body["location_id"] = self.need_location(inp["location_id"])
            changes.append(f"location {self.cat.location_name(body['location_id'])}")
        if inp.get("open") is not None:
            body["open"] = bool(inp["open"])
            changes.append("opened" if body["open"] else "not opened")
        if not changes:
            raise ToolError("No changes were given.")
        return [action("edit_entry", "edit", self.cat.product_name(pid), "Change to: " + ", ".join(changes), entry_id=entry_id, body=body)]

    async def p_undo_transactions(self, inp: dict) -> list[dict]:
        out = []
        for tid in inp.get("transaction_ids") or []:
            rows = await self.g.get(f"/stock/transactions/{tid}")
            if not rows:
                raise ToolError(f"There is no transaction {tid}.")
            if all(to_int(r.get("undone")) for r in rows):
                raise ToolError(f"Transaction {tid} was already undone.")
            first = rows[0]
            out.append(
                action("undo", "undo", self.cat.product_name(first["product_id"]),
                       f"Undo {str(first.get('transaction_type', 'booking')).replace('-', ' ')} of {self.cat.amount(first['product_id'], abs(sum(to_float(r['amount']) for r in rows)))}",
                       transaction_id=str(tid))
            )
        return out

    async def p_add_to_shopping_list(self, inp: dict) -> list[dict]:
        list_id = to_int(inp.get("list_id")) or 1
        out = []
        for it in self.items(inp):
            amount = self.need_amount(it.get("amount", 1))
            note = (it.get("note") or "").strip() or None
            if it.get("product_id"):
                p = await self.need_product(it["product_id"])
                detail = self.cat.amount(p["id"], amount) + (f" · {note}" if note else "")
                out.append(action("list_add", "list", p["name"], detail, product_id=int(p["id"]), amount=amount, note=note, list_id=list_id))
            elif note:
                out.append(action("list_add", "list", note, fmt_num(amount) if amount != 1 else "", product_id=None, amount=amount, note=note, list_id=list_id))
            else:
                raise ToolError("Each item needs product_id or note.")
        return out

    async def list_item(self, item_id: Any) -> tuple[dict, str]:
        rows = await self.g.objects("shopping_list", [f"id={to_int(item_id)}"])
        if not rows:
            raise ToolError(f"There is no shopping list item with id {item_id}. Call get_shopping_list for current ids.")
        row = rows[0]
        return row, self.cat.product_name(row["product_id"]) if row.get("product_id") else (row.get("note") or "item")

    async def p_remove_from_shopping_list(self, inp: dict) -> list[dict]:
        out = []
        for item_id in inp.get("item_ids") or []:
            row, name = await self.list_item(item_id)
            out.append(action("list_remove", "list", name, "Removed from the list", item_id=int(row["id"])))
        return out

    async def p_update_shopping_list_item(self, inp: dict) -> list[dict]:
        row, name = await self.list_item(inp.get("item_id"))
        body, changes = {}, []
        if inp.get("amount") is not None:
            body["amount"] = self.need_amount(inp["amount"])
            changes.append(f"amount {fmt_num(body['amount'])}")
        if inp.get("note") is not None:
            body["note"] = str(inp["note"])
            changes.append(f"note: {body['note']}")
        if inp.get("done") is not None:
            body["done"] = 1 if inp["done"] else 0
            changes.append("ticked off" if inp["done"] else "unticked")
        if not body:
            raise ToolError("No changes were given.")
        return [action("list_update", "list", name, ", ".join(changes).capitalize(), item_id=int(row["id"]), body=body)]

    async def p_add_low_stock_to_shopping_list(self, inp: dict) -> list[dict]:
        missing = (await self.g.get("/stock/volatile")).get("missing_products") or []
        if not missing:
            raise ToolError("No product is below its minimum stock; nothing to add.")
        names = ", ".join(m["name"] for m in missing[:12])
        return [action("list_add_missing", "list", "Low-stock products", names, list_id=to_int(inp.get("list_id")) or 1)]

    async def p_clear_shopping_list(self, inp: dict) -> list[dict]:
        list_id = to_int(inp.get("list_id")) or 1
        done_only = bool(inp.get("done_only"))
        rows = await self.g.objects("shopping_list", [f"shopping_list_id={list_id}"])
        count = len([r for r in rows if to_int(r.get("done"))] if done_only else rows)
        if not count:
            raise ToolError("There is nothing to remove from that list.")
        what = "ticked-off items" if done_only else "items"
        return [action("list_clear", "remove", "Shopping list", f"Remove {'the' if done_only else 'all'} {count} {what}", list_id=list_id, done_only=done_only)]

    async def p_create_product(self, inp: dict) -> list[dict]:
        fields = self.check_new_product(inp)
        detail = f"{self.cat.location_name(fields['location_id'])} · counted in {self.cat.unit_name(fields['qu_id_stock'], 2)}"
        return [action("create_product", "new", fields["name"], detail, fields=fields)]

    async def p_update_product(self, inp: dict) -> list[dict]:
        p = await self.need_product(inp.get("product_id"))
        fields, changes = {}, []
        if inp.get("name"):
            fields["name"] = str(inp["name"]).strip()
            changes.append(f"rename to {fields['name']}")
        if inp.get("description") is not None:
            fields["description"] = str(inp["description"])
            changes.append("new description")
        if inp.get("location_id"):
            fields["location_id"] = self.need_location(inp["location_id"])
            changes.append(f"kept in {self.cat.location_name(fields['location_id'])}")
        if inp.get("product_group_id"):
            if to_int(inp["product_group_id"]) not in self.cat.groups:
                raise ToolError("Unknown product group.")
            fields["product_group_id"] = to_int(inp["product_group_id"])
            changes.append(f"group {self.cat.groups[fields['product_group_id']]['name']}")
        if inp.get("store_id"):
            if to_int(inp["store_id"]) not in self.cat.stores:
                raise ToolError("Unknown store.")
            fields["shopping_location_id"] = to_int(inp["store_id"])
            changes.append(f"default store {self.cat.stores[fields['shopping_location_id']]['name']}")
        if inp.get("min_stock_amount") is not None:
            fields["min_stock_amount"] = to_float(inp["min_stock_amount"])
            changes.append(f"minimum stock {fmt_num(fields['min_stock_amount'])}")
        for key, label in (("default_best_before_days", "shelf life"), ("default_best_before_days_after_open", "shelf life after opening")):
            if inp.get(key) is not None:
                fields[key] = to_int(inp[key])
                changes.append(f"{label} {'never expires' if fields[key] == -1 else str(fields[key]) + ' days'}")
        if not fields:
            raise ToolError("No changes were given.")
        return [action("update_product", "edit", p["name"], "; ".join(changes).capitalize(), product_id=int(p["id"]), fields=fields)]

    async def p_create_master_item(self, inp: dict) -> list[dict]:
        kind, name = str(inp.get("kind")), str(inp.get("name", "")).strip()
        entity, existing, label = {
            "location": ("locations", self.cat.locations, "New location"),
            "quantity_unit": ("quantity_units", self.cat.units, "New unit"),
            "store": ("shopping_locations", self.cat.stores, "New store"),
            "product_group": ("product_groups", self.cat.groups, "New product group"),
        }.get(kind, (None, None, None))
        if not entity or not name:
            raise ToolError("kind and name are required.")
        clash = next((i for i, r in existing.items() if r["name"].strip().lower() == name.lower()), None)
        if clash:
            raise ToolError(f"{name!r} already exists with id {clash}.")
        body: dict[str, Any] = {"name": name}
        if kind == "quantity_unit":
            body["name_plural"] = str(inp.get("name_plural") or name)
        if kind == "location":
            body["is_freezer"] = 1 if inp.get("is_freezer") else 0
        return [action("create_master", "new", name, label, entity=entity, body=body)]

    async def recipe(self, recipe_id: Any) -> dict:
        try:
            recipe = await self.g.get(f"/objects/recipes/{to_int(recipe_id)}")
        except GrocyError:
            recipe = None
        if not recipe:
            raise ToolError(f"There is no recipe with id {recipe_id}.")
        return recipe

    async def p_consume_recipe(self, inp: dict) -> list[dict]:
        recipe = await self.recipe(inp.get("recipe_id"))
        positions = await self.g.objects("recipes_pos", [f"recipe_id={recipe['id']}"])
        names = ", ".join(self.cat.product_name(p["product_id"]) for p in positions[:12])
        return [action("recipe_consume", "remove", recipe["name"], f"Use up its ingredients: {names}", recipe_id=int(recipe["id"]))]

    async def p_add_recipe_missing_to_shopping_list(self, inp: dict) -> list[dict]:
        recipe = await self.recipe(inp.get("recipe_id"))
        return [action("recipe_to_list", "list", recipe["name"], "Missing ingredients added to the list", recipe_id=int(recipe["id"]))]

    async def p_create_recipe(self, inp: dict) -> list[dict]:
        name = str(inp.get("name", "")).strip()
        if not name:
            raise ToolError("The recipe needs a name.")
        ingredients = []
        for ing in inp.get("ingredients") or []:
            p = await self.need_product(ing.get("product_id"))
            ingredients.append({"product_id": int(p["id"]), "amount": self.need_amount(ing.get("amount")), "qu_id": to_int(p.get("qu_id_stock")), "note": ing.get("note") or None})
        if not ingredients:
            raise ToolError("The recipe needs at least one ingredient.")
        detail = ", ".join(f"{self.cat.amount(i['product_id'], i['amount'])} {self.cat.product_name(i['product_id'])}" for i in ingredients[:12])
        return [action("create_recipe", "new", name, detail, name=name, instructions=str(inp.get("instructions") or ""), servings=to_int(inp.get("servings")) or 1, ingredients=ingredients)]

    async def p_add_meal_plan_entry(self, inp: dict) -> list[dict]:
        day = parse_date(inp.get("day"), "day")
        if not day:
            raise ToolError("day is required.")
        servings = to_float(inp.get("servings")) or 1
        if inp.get("recipe_id"):
            recipe = await self.recipe(inp["recipe_id"])
            title, body = recipe["name"], {"type": "recipe", "recipe_id": int(recipe["id"]), "recipe_servings": servings}
        elif inp.get("product_id"):
            p = await self.need_product(inp["product_id"])
            title, body = p["name"], {"type": "product", "product_id": int(p["id"]), "product_amount": servings, "product_qu_id": to_int(p.get("qu_id_stock"))}
        elif inp.get("note"):
            title, body = str(inp["note"]), {"type": "note", "note": str(inp["note"])}
        else:
            raise ToolError("Give recipe_id, product_id or note.")
        return [action("meal_plan_add", "list", title, f"Planned for {nice_date(day)}", body={**body, "day": day})]

    async def p_track_chore(self, inp: dict) -> list[dict]:
        cid = to_int(inp.get("chore_id"))
        chore = next((c for c in await self.g.get("/chores") if to_int(c["chore_id"]) == cid), None)
        if not chore:
            raise ToolError(f"There is no chore with id {cid}.")
        return [action("chore_track", "list", chore["chore_name"], "Marked as done", chore_id=cid)]

    async def p_add_task(self, inp: dict) -> list[dict]:
        name = str(inp.get("name", "")).strip()
        if not name:
            raise ToolError("The task needs a name.")
        due = parse_date(inp.get("due_date"), "due_date")
        body = {"name": name, "description": str(inp.get("description") or ""), **({"due_date": due} if due else {})}
        return [action("task_add", "list", name, f"Task added{', due ' + nice_date(due) if due else ''}", body=body)]

    async def p_complete_task(self, inp: dict) -> list[dict]:
        tid = to_int(inp.get("task_id"))
        task = next((t for t in await self.g.get("/tasks") if to_int(t["id"]) == tid), None)
        if not task:
            raise ToolError(f"There is no open task with id {tid}.")
        return [action("task_complete", "list", task["name"], "Task completed", task_id=tid)]

    # ------------------------------------------------ write tools: execute

    async def execute(self, act: dict) -> dict:
        """Run one planned action. Never raises: returns {ok, summary|error, transaction_id?}."""
        try:
            result = await getattr(self, f"x_{act['op']}")(**act["args"])
            return {"ok": True, **result}
        except GrocyError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # keep one bad item from sinking the rest
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def booked(rows: Any, summary: str, **extra: Any) -> dict:
        tid = rows[0].get("transaction_id") if isinstance(rows, list) and rows else None
        return {"summary": summary, **({"transaction_id": tid} if tid else {}), **extra}

    async def x_purchase(self, product_id, new_product, amount, price, best_before_date, location_id, store_id, purchased_date, note) -> dict:
        extra = {}
        if new_product:
            product_id = await self.g.create("products", new_product)
            await self.cat.refresh(force=True)
            extra = {"created_product_id": product_id}
        body = {"amount": amount, "transaction_type": "purchase", "best_before_date": best_before_date}
        for key, value in (("price", price), ("location_id", location_id), ("shopping_location_id", store_id), ("purchased_date", purchased_date), ("note", note)):
            if value is not None:
                body[key] = value
        rows = await self.g.post(f"/stock/products/{product_id}/add", body)
        try:
            await self.g.post("/stock/shoppinglist/remove-product", {"product_id": product_id, "product_amount": amount})
        except GrocyError:
            pass
        return self.booked(rows, f"Added {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)}", product_id=product_id, **extra)

    async def x_consume(self, product_id, amount, spoiled, location_id) -> dict:
        body = {"amount": amount, "transaction_type": "consume", "spoiled": spoiled}
        if location_id:
            body["location_id"] = location_id
        rows = await self.g.post(f"/stock/products/{product_id}/consume", body)
        left = await self.stock_amount(product_id)
        return self.booked(rows, f"Took {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)} out of stock; {self.cat.amount(product_id, left)} left", product_id=product_id)

    async def x_open(self, product_id, amount) -> dict:
        rows = await self.g.post(f"/stock/products/{product_id}/open", {"amount": amount})
        return self.booked(rows, f"Marked {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)} as opened")

    async def x_inventory(self, product_id, new_amount, best_before_date, location_id) -> dict:
        body = {"new_amount": new_amount, "best_before_date": best_before_date}
        if location_id:
            body["location_id"] = location_id
        rows = await self.g.post(f"/stock/products/{product_id}/inventory", body)
        return self.booked(rows, f"{self.cat.product_name(product_id)} stock set to {self.cat.amount(product_id, new_amount)}")

    async def x_transfer(self, product_id, amount, location_id_from, location_id_to) -> dict:
        rows = await self.g.post(f"/stock/products/{product_id}/transfer", {"amount": amount, "location_id_from": location_id_from, "location_id_to": location_id_to})
        return self.booked(rows, f"Moved {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)} to {self.cat.location_name(location_id_to)}")

    async def x_edit_entry(self, entry_id, body) -> dict:
        await self.g.put(f"/stock/entry/{entry_id}", body)
        return {"summary": f"Stock entry {entry_id} updated"}

    async def x_undo(self, transaction_id) -> dict:
        await self.g.post(f"/stock/transactions/{transaction_id}/undo")
        return {"summary": f"Transaction {transaction_id} undone"}

    async def x_list_add(self, product_id, amount, note, list_id) -> dict:
        if product_id:
            body = {"product_id": product_id, "list_id": list_id, "product_amount": amount}
            if note:
                body["note"] = note
            await self.g.post("/stock/shoppinglist/add-product", body)
            return {"summary": f"{self.cat.product_name(product_id)} is on the shopping list"}
        await self.g.create("shopping_list", {"note": note, "amount": amount, "shopping_list_id": list_id})
        return {"summary": f"{note} is on the shopping list"}

    async def x_list_remove(self, item_id) -> dict:
        await self.g.request("DELETE", f"/objects/shopping_list/{item_id}")
        return {"summary": f"Shopping list item {item_id} removed"}

    async def x_list_update(self, item_id, body) -> dict:
        await self.g.put(f"/objects/shopping_list/{item_id}", body)
        return {"summary": f"Shopping list item {item_id} updated"}

    async def x_list_add_missing(self, list_id) -> dict:
        await self.g.post("/stock/shoppinglist/add-missing-products", {"list_id": list_id})
        return {"summary": "Low-stock products are on the shopping list"}

    async def x_list_clear(self, list_id, done_only) -> dict:
        await self.g.post("/stock/shoppinglist/clear", {"list_id": list_id, "done_only": done_only})
        return {"summary": "Shopping list cleared"}

    async def x_create_product(self, fields) -> dict:
        pid = await self.g.create("products", fields)
        await self.cat.refresh(force=True)
        return {"summary": f"Created product {fields['name']}", "created_product_id": pid}

    async def x_update_product(self, product_id, fields) -> dict:
        await self.g.put(f"/objects/products/{product_id}", fields)
        await self.cat.refresh(force=True)
        return {"summary": f"Updated {self.cat.product_name(product_id)}"}

    async def x_create_master(self, entity, body) -> dict:
        new_id = await self.g.create(entity, body)
        await self.cat.refresh(force=True)
        return {"summary": f"Created {body['name']}", "created_id": new_id}

    async def x_recipe_consume(self, recipe_id) -> dict:
        await self.g.post(f"/recipes/{recipe_id}/consume")
        return {"summary": "Recipe ingredients taken out of stock"}

    async def x_recipe_to_list(self, recipe_id) -> dict:
        await self.g.post(f"/recipes/{recipe_id}/add-not-fulfilled-products-to-shoppinglist")
        return {"summary": "Missing ingredients are on the shopping list"}

    async def x_create_recipe(self, name, instructions, servings, ingredients) -> dict:
        html = "".join(f"<p>{line}</p>" for line in instructions.replace("<", "&lt;").splitlines() if line.strip())
        rid = await self.g.create("recipes", {"name": name, "description": html, "base_servings": servings})
        for ing in ingredients:
            await self.g.create("recipes_pos", {"recipe_id": rid, **{k: v for k, v in ing.items() if v is not None}})
        return {"summary": f"Saved recipe {name}", "created_recipe_id": rid}

    async def x_meal_plan_add(self, body) -> dict:
        await self.g.create("meal_plan", body)
        return {"summary": f"Meal planned for {body['day']}"}

    async def x_chore_track(self, chore_id) -> dict:
        await self.g.post(f"/chores/{chore_id}/execute")
        return {"summary": "Chore recorded as done"}

    async def x_task_add(self, body) -> dict:
        tid = await self.g.create("tasks", body)
        return {"summary": f"Task added: {body['name']}", "created_task_id": tid}

    async def x_task_complete(self, task_id) -> dict:
        await self.g.post(f"/tasks/{task_id}/complete")
        return {"summary": "Task completed"}
