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
from pathlib import Path
from typing import Any

from .grocy import Catalog, Grocy, GrocyError, actor_email, fmt_num, to_float, to_int

GUIDE = (Path(__file__).parent / "grocy_guide.md").read_text()

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
        "default_best_before_days_after_open": d(INT, "Shelf life in days once opened."),
        "store_id": d(INT, "Store it is usually bought at."),
        "description": STR,
        "parent_product_id": d(INT, "Generic parent product this is a variant of."),
    },
    ["name", "location_id", "qu_id"],
)


# kind -> (Grocy entity, word shown to the user)
MASTER = {
    "location": ("locations", "location"),
    "quantity_unit": ("quantity_units", "unit"),
    "store": ("shopping_locations", "store"),
    "product_group": ("product_groups", "product group"),
    "shopping_list": ("shopping_lists", "shopping list"),
    "meal_plan_section": ("meal_plan_sections", "meal plan section"),
    "task_category": ("task_categories", "task category"),
    "battery": ("batteries", "battery"),
    "equipment": ("equipment", "equipment"),
}
MASTER_FIELDS = {
    "name": STR,
    "name_plural": d(STR, "Quantity units only."),
    "is_freezer": d(BOOL, "Locations only."),
    "description": d(STR, "Not for meal plan sections."),
    "charge_interval_days": d(INT, "Batteries only: remind to charge every N days."),
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
CHORE_FIELDS = {
    "name": STR,
    "description": STR,
    "schedule": {"type": "string", "enum": ["manually", "daily", "weekly", "monthly", "yearly"],
                 "description": "manually = no schedule. daily/weekly/monthly/yearly repeat every `every` days/weeks/months/years."},
    "every": d(INT, "Repeat interval, default 1 (e.g. schedule=daily, every=3 is every 3 days)."),
    "weekdays": {"type": "array", "items": {"type": "string", "enum": WEEKDAYS}, "description": "Weekly schedules: which days."},
    "day_of_month": d(INT, "Monthly schedules: 1-31."),
    "start_date": DATE,
    "assignment": {"type": "string", "enum": ["no-assignment", "who-least-did-first", "random", "in-alphabetical-order"]},
    "assigned_user_ids": {"type": "array", "items": INT, "description": "People who take turns (ids from get_master_data users)."},
}
INGREDIENTS = arr(
    obj(
        {
            "product_id": INT,
            "amount": d(NUM, "In the product's stock unit."),
            "note": STR,
            "variable_amount": d(STR, "Free text shown instead of the amount, e.g. 'a pinch'."),
            "only_check_any_in_stock": d(BOOL, "True for staples like salt or oil: any amount in stock counts as enough."),
            "group": d(STR, "Optional heading, e.g. 'For the sauce'."),
        },
        ["product_id", "amount"],
    )
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
        "Fuzzy-search products by name, or look one up by its barcode digits. Returns the closest matches with current stock. "
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
        "stock, barcodes, parent product, last and average price and recent price history. Use before "
        "converting between units.",
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
        "Items on the shopping lists with their item ids, amounts, notes and done state.",
        obj({"list_id": d(INT, "Omit to read every list.")}),
        "Reading the shopping list",
    ),
    Tool(
        "get_master_data",
        "Fresh list of locations, quantity units, stores, product groups, shopping lists, meal plan "
        "sections (breakfast, dinner...), task categories, household members (users), batteries or equipment.",
        obj({"kind": {"type": "string", "enum": ["locations", "quantity_units", "stores", "product_groups", "shopping_lists",
                                                 "meal_plan_sections", "task_categories", "users", "batteries", "equipment"]}}, ["kind"]),
        "Checking Grocy settings",
    ),
    Tool(
        "grocy_help",
        "Reference guide to Grocy itself: what its features are, how they work and where to find them "
        "in Grocy's menus, plus this household's Grocy version and which features are switched on. "
        "Call this before answering any question about Grocy as an app (\"can Grocy do X\", \"how do "
        "minimum stock amounts work\", \"where do I change Y\") instead of answering from memory.",
        obj({}),
        "Reading the Grocy guide",
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
                            "entry_id": d(INT, "Only take from this stock entry (ids from get_stock with product_id)."),
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
        "Change the amount or note of a shopping list item, tick it off / untick it, or move it to another list. Runs immediately.",
        obj({"item_id": INT, "amount": NUM, "note": STR, "done": BOOL, "list_id": d(INT, "Move the item to this list.")}, ["item_id"]),
        "Updating the shopping list",
        "auto",
    ),
    Tool(
        "add_low_stock_to_shopping_list",
        "Put every product that is below its minimum stock on the shopping list, optionally also the "
        "overdue and expired ones (to replace them). Runs immediately.",
        obj({"list_id": d(INT, "Default 1."), "also_overdue": BOOL, "also_expired": BOOL}),
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
                "default_best_before_days_after_freezing": INT,
                "default_best_before_days_after_thawing": INT,
                "store_id": d(INT, "Default store."),
                "due_type": {"type": "string", "enum": ["best_before", "expiration"], "description": "best_before: still usable after the date. expiration: unsafe after the date."},
                "parent_product_id": d(INT, "Make this a variant of a generic parent product; 0 removes the parent."),
                "active": d(BOOL, "false hides a product that is no longer used (it is not deleted)."),
                "quick_consume_amount": d(NUM, "Amount Grocy's one-tap consume button takes."),
                "calories": d(NUM, "Per stock unit."),
                "treat_opened_as_out_of_stock": BOOL,
                "should_not_be_frozen": BOOL,
                "hide_on_stock_overview": BOOL,
            },
            ["product_id"],
        ),
        "Preparing product change",
        "gated",
    ),
    Tool(
        "create_master_item",
        "Create a location, quantity unit, store, product group, shopping list, meal plan section, "
        "task category, battery or piece of equipment. The user confirms.",
        obj({"kind": {"type": "string", "enum": list(MASTER)}, **MASTER_FIELDS}, ["kind", "name"]),
        "Preparing new entry",
        "gated",
    ),
    Tool(
        "update_master_item",
        "Rename or edit a location, quantity unit, store, product group, shopping list, meal plan "
        "section, task category, battery or piece of equipment. Only the given fields change. The user confirms.",
        obj({"kind": {"type": "string", "enum": list(MASTER)}, "id": INT, **MASTER_FIELDS}, ["kind", "id"]),
        "Preparing change",
        "gated",
    ),
    Tool(
        "add_product_barcode",
        "Attach a barcode to a product so it can be found by scanning. The user confirms.",
        obj({"product_id": INT, "barcode": STR, "amount": d(NUM, "How many stock units one scan of this pack means."), "store_id": INT, "note": STR}, ["product_id", "barcode"]),
        "Preparing barcode",
        "gated",
    ),
    Tool(
        "add_unit_conversion",
        "Teach Grocy that 1 of one unit equals `factor` of another (e.g. 1 pack = 12 pieces). With "
        "product_id it applies to that product only, otherwise everywhere. The user confirms.",
        obj({"from_unit_id": INT, "to_unit_id": INT, "factor": NUM, "product_id": INT}, ["from_unit_id", "to_unit_id", "factor"]),
        "Preparing unit conversion",
        "gated",
    ),
    Tool(
        "get_recipes",
        "Recipes saved in Grocy with whether current stock covers them. With recipe_id: the ingredients "
        "(and how much of each is missing) and instructions.",
        obj({"recipe_id": INT}),
        "Looking at recipes",
        feature="FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "consume_recipe",
        "Take all ingredients of a saved recipe out of stock (it was cooked). The user confirms.",
        obj({"recipe_id": INT, "servings": d(NUM, "How many servings were cooked. Default: the recipe's own.")}, ["recipe_id"]),
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
                "ingredients": INGREDIENTS,
            },
            ["name", "ingredients"],
        ),
        "Preparing recipe",
        "gated",
        "FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "update_recipe",
        "Edit a saved recipe: rename it, change the instructions or servings, and add or change "
        "ingredients. Each given ingredient is matched by product: one already in the recipe is "
        "changed, a new one is added, the others stay as they are. Ingredients cannot be removed "
        "here; that has to be done in Grocy. The user confirms.",
        obj(
            {
                "recipe_id": INT,
                "name": STR,
                "instructions": d(STR, "Plain text, one step per line."),
                "servings": INT,
                "ingredients": INGREDIENTS,
            },
            ["recipe_id"],
        ),
        "Preparing recipe change",
        "gated",
        "FEATURE_FLAG_RECIPES",
    ),
    Tool(
        "get_meal_plan",
        "Meal plan entries (with entry ids) between two dates (default: today and the next 7 days).",
        obj({"from_date": DATE, "to_date": DATE}),
        "Looking at the meal plan",
        feature="FEATURE_FLAG_RECIPES_MEALPLAN",
    ),
    Tool(
        "add_meal_plan_entry",
        "Plan a meal for a day: a saved recipe, a product, or a free-text note. Runs immediately.",
        obj({"day": DATE, "recipe_id": INT, "product_id": INT, "note": STR, "servings": d(NUM, "Default 1."),
             "section_id": d(INT, "Meal plan section such as breakfast or dinner (get_master_data meal_plan_sections).")}, ["day"]),
        "Updating the meal plan",
        "auto",
        "FEATURE_FLAG_RECIPES_MEALPLAN",
    ),
    Tool(
        "update_meal_plan_entry",
        "Change a meal plan entry: move it to another day or section, change servings or the note, or "
        "mark it done. Entries cannot be removed here. Runs immediately.",
        obj({"entry_id": INT, "day": DATE, "servings": NUM, "section_id": INT, "note": STR, "done": BOOL}, ["entry_id"]),
        "Updating the meal plan",
        "auto",
        "FEATURE_FLAG_RECIPES_MEALPLAN",
    ),
    Tool(
        "get_chores",
        "Household chores with schedule, who is next and when each is next due. With chore_id: its "
        "details and recent times it was done (with execution ids for undo_chore).",
        obj({"chore_id": INT}),
        "Looking at chores",
        feature="FEATURE_FLAG_CHORES",
    ),
    Tool(
        "track_chore",
        "Record that a chore was done (now, or on done_on), or skipped this time. Runs immediately.",
        obj({"chore_id": INT, "done_on": d(DATE, "Omit for now."), "skipped": BOOL,
             "done_by_user_id": d(INT, "Who did it, if not the person writing (get_master_data users).")}, ["chore_id"]),
        "Recording chore",
        "auto",
        "FEATURE_FLAG_CHORES",
    ),
    Tool(
        "undo_chore",
        "Undo a recorded chore execution by execution id (from track_chore's result or get_chores with chore_id). Runs immediately.",
        obj({"execution_id": INT}, ["execution_id"]),
        "Recording chore",
        "auto",
        "FEATURE_FLAG_CHORES",
    ),
    Tool(
        "create_chore",
        "Set up a new recurring chore. The user confirms.",
        obj(CHORE_FIELDS, ["name", "schedule"]),
        "Preparing chore",
        "gated",
        "FEATURE_FLAG_CHORES",
    ),
    Tool(
        "update_chore",
        "Edit a chore: name, description, schedule or who does it. active=false retires it. Only the given fields change. The user confirms.",
        obj({"chore_id": INT, **CHORE_FIELDS, "active": BOOL}, ["chore_id"]),
        "Preparing chore change",
        "gated",
        "FEATURE_FLAG_CHORES",
    ),
    Tool(
        "get_tasks",
        "To-do tasks with due date, category and who they are assigned to. Open ones by default.",
        obj({"include_done": d(BOOL, "Also list recently completed tasks.")}),
        "Looking at tasks",
        feature="FEATURE_FLAG_TASKS",
    ),
    Tool(
        "add_task",
        "Add a to-do task. Runs immediately.",
        obj({"name": STR, "due_date": DATE, "description": STR, "category_id": INT,
             "assigned_to_user_id": d(INT, "From get_master_data users.")}, ["name"]),
        "Adding task",
        "auto",
        "FEATURE_FLAG_TASKS",
    ),
    Tool(
        "update_task",
        "Change a task's name, due date (empty string clears it), description, category or assignee. Runs immediately.",
        obj({"task_id": INT, "name": STR, "due_date": STR, "description": STR, "category_id": INT, "assigned_to_user_id": INT}, ["task_id"]),
        "Updating task",
        "auto",
        "FEATURE_FLAG_TASKS",
    ),
    Tool(
        "reopen_task",
        "Mark a completed task as not done again. Runs immediately.",
        obj({"task_id": INT}, ["task_id"]),
        "Updating task",
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
        "get_calendar",
        "Everything with a date between two days (default: today and the next 14 days): products "
        "reaching their due date, planned meals, chores, tasks and battery charges.",
        obj({"from_date": DATE, "to_date": DATE}),
        "Looking at the calendar",
    ),
    Tool(
        "get_spending",
        "Money spent on purchases between two dates (default: the last 30 days): total, per store, "
        "per product group and the biggest items. Only purchases that were entered with a price count.",
        obj({"from_date": DATE, "to_date": DATE}),
        "Adding up spending",
    ),
    Tool("get_batteries", "Rechargeable batteries with last and next charge.", obj({}), "Looking at batteries", feature="FEATURE_FLAG_BATTERIES"),
    Tool(
        "charge_battery",
        "Record that a battery was charged (now, or on charged_on). Runs immediately.",
        obj({"battery_id": INT, "charged_on": DATE}, ["battery_id"]),
        "Recording charge",
        "auto",
        "FEATURE_FLAG_BATTERIES",
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


def recipe_html(instructions: str) -> str:
    return "".join(f"<p>{line}</p>" for line in instructions.replace("<", "&lt;").splitlines() if line.strip())


def cap(text: str) -> str:
    """Upper-case the first letter only (str.capitalize would lower-case names in the rest)."""
    return text[:1].upper() + text[1:]


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


def signed(note: str | None) -> str | None:
    """Grocy books every API change under the API key's owner, so name the person in the note."""
    who = actor_email.get()
    if not who or who.endswith("@local"):
        return note
    return f"{note} (via Pantry: {who})" if note else f"via Pantry: {who}"


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

    async def need_row(self, entity: str, row_id: Any, what: str) -> dict:
        rid = to_int(row_id)
        rows = await self.g.objects(entity, [f"id={rid}"]) if rid else []
        if not rows:
            raise ToolError(f"There is no {what} with id {row_id}.")
        return rows[0]

    async def users(self) -> dict[int, str]:
        try:
            return {int(u["id"]): u.get("display_name") or u.get("username") or f"user {u['id']}" for u in await self.g.get("/users")}
        except GrocyError:
            return {}

    async def need_user(self, user_id: Any) -> tuple[int, str]:
        users = await self.users()
        if to_int(user_id) not in users:
            raise ToolError(f"There is no user with id {user_id}. Known: {users}")
        return to_int(user_id), users[to_int(user_id)]

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
        if np.get("default_best_before_days_after_open") is not None:
            fields["default_best_before_days_after_open"] = to_int(np["default_best_before_days_after_open"]) or 0
        if np.get("store_id"):
            if to_int(np["store_id"]) not in self.cat.stores:
                raise ToolError(f"There is no store with id {np['store_id']}.")
            fields["shopping_location_id"] = to_int(np["store_id"])
        if np.get("description"):
            fields["description"] = str(np["description"])
        if np.get("parent_product_id"):
            if not self.cat.product(np["parent_product_id"]):
                raise ToolError(f"There is no product with id {np['parent_product_id']} to use as parent.")
            fields["parent_product_id"] = to_int(np["parent_product_id"])
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
        if re.fullmatch(r"[\d\s-]{6,}", query):
            code = re.sub(r"\D", "", query)
            hit = await self.g.objects("product_barcodes", [f"barcode={code}"])
            if not hit:
                return "No product has that barcode yet. If the user says which product it is, add_product_barcode can attach it."
            pid = to_int(hit[0]["product_id"])
            return [{"product_id": pid, "name": self.cat.product_name(pid), "in_stock": self.cat.amount(pid, await self.stock_amount(pid))}]
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
        try:
            history = await self.g.get(f"/stock/products/{pid}/price-history") or []
        except GrocyError:
            history = []
        return {
            "active": to_int(p.get("active", 1)) != 0,
            "parent_product": self.cat.product_name(p["parent_product_id"]) if p.get("parent_product_id") else None,
            "product_group": self.cat.groups.get(to_int(p.get("product_group_id")) or -1, {}).get("name"),
            "default_store": self.cat.stores.get(to_int(p.get("shopping_location_id")) or -1, {}).get("name"),
            "due_type": "expiration" if to_int(p.get("due_type")) == 2 else "best_before",
            "last_used": details.get("last_used"),
            "price_history": [
                {"date": str(h.get("date"))[:10], "price_per_unit": h.get("price"), "store": (h.get("shopping_location") or {}).get("name")}
                for h in history[-6:]
            ],
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
            "calories_per_unit": p.get("calories"),
            "quick_consume_amount": p.get("quick_consume_amount"),
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
        list_id = to_int(inp.get("list_id"))
        if list_id and list_id not in self.cat.lists:
            raise ToolError(f"There is no shopping list with id {list_id}.")
        rows = await self.g.objects("shopping_list", [f"shopping_list_id={list_id}"] if list_id else None)
        out = []
        for r in rows:
            pid = to_int(r.get("product_id"))
            out.append(
                {
                    "list": self.cat.lists.get(to_int(r.get("shopping_list_id")) or -1, {}).get("name"),
                    "item_id": to_int(r["id"]),
                    "product_id": pid,
                    "name": self.cat.product_name(pid) if pid else (r.get("note") or "(empty)"),
                    "amount": self.cat.amount(pid, r["amount"]) if pid else fmt_num(r["amount"]),
                    "note": r.get("note") if pid else None,
                    "done": bool(to_int(r.get("done"))),
                }
            )
        return out or ("That shopping list is empty." if list_id else "The shopping lists are empty.")

    async def r_get_master_data(self, inp: dict) -> Any:
        await self.cat.refresh(force=True)
        kind = str(inp.get("kind"))
        if kind == "users":
            return [{"id": i, "name": n} for i, n in sorted((await self.users()).items())] or "No users could be read."
        if kind in ("meal_plan_sections", "task_categories", "equipment"):
            rows = await self.g.objects(kind, order="name")
            return [{"id": to_int(r["id"]), "name": r["name"], **({"description": strip_html(r["description"])} if r.get("description") else {})} for r in rows if r.get("name")] or "None yet."
        if kind == "batteries":
            return await self.r_get_batteries({})
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

    async def r_grocy_help(self, inp: dict) -> Any:
        try:
            info = await self.g.get("/system/info") or {}
            version = (info.get("grocy_version") or {}).get("Version") or "unknown"
        except GrocyError:
            version = "unknown"
        flags = {k: bool(v) for k, v in sorted(self.cat.config.items()) if k.startswith("FEATURE_FLAG_")}
        return {
            "this_household": {
                "grocy_version": version,
                "currency": self.cat.currency,
                "features_switched_off": [k.removeprefix("FEATURE_FLAG_") for k, on in flags.items() if not on] or "none",
            },
            "guide": GUIDE,
        }

    async def r_get_recipes(self, inp: dict) -> Any:
        rid = to_int(inp.get("recipe_id"))
        if rid:
            recipe = await self.recipe(rid)
            positions = await self.g.objects("recipes_pos", [f"recipe_id={rid}"])
            try:
                resolved = {to_int(r["product_id"]): r for r in await self.g.objects("recipes_pos_resolved", [f"recipe_id={rid}"])}
            except GrocyError:
                resolved = {}

            def missing(p: dict) -> str | None:
                amount = to_float(resolved.get(to_int(p["product_id"]), {}).get("missing_amount"))
                return self.cat.amount(p["product_id"], amount) if amount > 0 else None

            return {
                "recipe_id": rid,
                "name": recipe["name"],
                "servings": recipe.get("base_servings"),
                "instructions": strip_html(recipe.get("description")),
                "ingredients": [
                    {k: v for k, v in {
                        "product_id": to_int(p["product_id"]), "name": self.cat.product_name(p["product_id"]),
                        "amount": to_float(p["amount"]), "unit": self.cat.unit_name(p.get("qu_id"), p["amount"]),
                        "note": p.get("note") or None, "variable_amount": p.get("variable_amount") or None,
                        "only_check_any_in_stock": bool(to_int(p.get("only_check_single_unit_in_stock"))) or None,
                        "group": p.get("ingredient_group") or None, "missing": missing(p),
                    }.items() if v is not None}
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

    async def sections(self) -> dict[int, str]:
        return {to_int(r["id"]): r["name"] for r in await self.g.objects("meal_plan_sections") if r.get("name")}

    async def meal_title(self, r: dict) -> str:
        if r.get("type") == "recipe" and r.get("recipe_id"):
            try:
                return (await self.g.get(f"/objects/recipes/{r['recipe_id']}"))["name"]
            except GrocyError:
                return "recipe"
        if r.get("type") == "product":
            return self.cat.product_name(r.get("product_id"))
        return r.get("note") or "note"

    async def r_get_meal_plan(self, inp: dict) -> Any:
        start = parse_date(inp.get("from_date"), "from_date") or self.today().isoformat()
        end = parse_date(inp.get("to_date"), "to_date") or (self.today() + timedelta(days=7)).isoformat()
        rows = await self.g.objects("meal_plan", [f"day>={start}", f"day<={end}"], order="day")
        sections = await self.sections()
        out = []
        for r in rows:
            row = {"entry_id": to_int(r["id"]), "day": r["day"], "type": r.get("type"), "what": await self.meal_title(r)}
            if r.get("type") == "recipe":
                row["recipe_id"], row["servings"] = to_int(r.get("recipe_id")), to_float(r.get("recipe_servings"))
            if sections.get(to_int(r.get("section_id")) or -1):
                row["section"] = sections[to_int(r["section_id"])]
            if to_int(r.get("done")):
                row["done"] = True
            out.append(row)
        return out or "Nothing is planned in that period."

    @staticmethod
    def chore_schedule(c: dict) -> str:
        kind, every = c.get("period_type") or "manually", to_int(c.get("period_interval")) or 1
        if kind == "manually":
            return "no schedule"
        unit = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}.get(kind, kind)
        text = f"every {unit}" if every == 1 else f"every {every} {unit}s"
        if kind == "weekly" and c.get("period_config"):
            text += " on " + ", ".join(str(c["period_config"]).split(","))
        if kind == "monthly" and to_int(c.get("period_days")):
            text += f" on day {to_int(c['period_days'])}"
        return text

    async def r_get_chores(self, inp: dict) -> Any:
        users = await self.users()
        cid = to_int(inp.get("chore_id"))
        if cid:
            chore = await self.need_row("chores", cid, "chore")
            log = await self.g.objects("chores_log", [f"chore_id={cid}"], order="id:desc", limit=10)
            return {
                "chore_id": cid, "name": chore["name"], "description": strip_html(chore.get("description")),
                "schedule": self.chore_schedule(chore), "active": to_int(chore.get("active", 1)) != 0,
                "assignment": chore.get("assignment_type"),
                "recent": [
                    {"execution_id": to_int(r["id"]), "when": r.get("tracked_time"), "by": users.get(to_int(r.get("done_by_user_id")) or -1),
                     "skipped": bool(to_int(r.get("skipped"))), "undone": bool(to_int(r.get("undone")))}
                    for r in log
                ],
            }
        defs = {to_int(c["id"]): c for c in await self.g.objects("chores")}
        return [
            {
                "chore_id": to_int(c["chore_id"]), "name": c["chore_name"],
                "schedule": self.chore_schedule(defs.get(to_int(c["chore_id"]), {})),
                "next_due": c.get("next_estimated_execution_time"), "last_done": c.get("last_tracked_time"),
                "next_person": users.get(to_int(c.get("next_execution_assigned_to_user_id")) or -1),
            }
            for c in await self.g.get("/chores")
        ] or "No chores are set up."

    async def r_get_tasks(self, inp: dict) -> Any:
        users = await self.users()
        categories = {to_int(c["id"]): c["name"] for c in await self.g.objects("task_categories")}

        def row(t: dict) -> dict:
            out = {"task_id": to_int(t["id"]), "name": t["name"], "due_date": t.get("due_date") or None}
            if strip_html(t.get("description")):
                out["description"] = strip_html(t.get("description"))
            if categories.get(to_int(t.get("category_id")) or -1):
                out["category"] = categories[to_int(t["category_id"])]
            if users.get(to_int(t.get("assigned_to_user_id")) or -1):
                out["assigned_to"] = users[to_int(t["assigned_to_user_id"])]
            return out

        open_tasks = [row(t) for t in await self.g.get("/tasks")]
        if not inp.get("include_done"):
            return open_tasks or "No open tasks."
        done = await self.g.objects("tasks", ["done=1"], order="done_timestamp:desc", limit=20)
        return {"open": open_tasks, "recently_completed": [{**row(t), "completed": t.get("done_timestamp")} for t in done]}

    async def r_get_batteries(self, inp: dict) -> Any:
        return [
            {"battery_id": to_int(b["battery_id"]), "name": (b.get("battery") or {}).get("name"), "used_in": (b.get("battery") or {}).get("used_in"),
             "last_charged": b.get("last_tracked_time"), "next_charge": b.get("next_estimated_charge_time")}
            for b in await self.g.get("/batteries")
        ] or "No batteries are set up."

    async def r_get_calendar(self, inp: dict) -> Any:
        start = parse_date(inp.get("from_date"), "from_date") or self.today().isoformat()
        end = parse_date(inp.get("to_date"), "to_date") or (self.today() + timedelta(days=14)).isoformat()

        def inside(value: Any) -> bool:
            return bool(value) and start <= str(value)[:10] <= end

        due: dict[tuple[str, int], float] = {}
        for e in await self.g.objects("stock", [f"best_before_date>={start}", f"best_before_date<={end}"]):
            key = (e["best_before_date"][:10], to_int(e["product_id"]))
            due[key] = due.get(key, 0) + to_float(e["amount"])
        out: dict[str, Any] = {
            "from": start, "to": end,
            "products_due": [{"day": day, "product": self.cat.product_name(pid), "amount": self.cat.amount(pid, amount)} for (day, pid), amount in sorted(due.items())],
        }
        if self.cat.feature("FEATURE_FLAG_RECIPES_MEALPLAN") and self.cat.feature("FEATURE_FLAG_RECIPES"):
            meals = await self.r_get_meal_plan({"from_date": start, "to_date": end})
            out["meals"] = meals if isinstance(meals, list) else []
        if self.cat.feature("FEATURE_FLAG_TASKS"):
            tasks = [{"day": str(t["due_date"])[:10], "task_id": to_int(t["id"]), "name": t["name"]} for t in await self.g.get("/tasks") if t.get("due_date")]
            out["tasks"] = [t for t in tasks if start <= t["day"] <= end]
            out["tasks_overdue"] = [t for t in tasks if t["day"] < start]
        if self.cat.feature("FEATURE_FLAG_CHORES"):
            out["chores"] = [{"day": str(c["next_estimated_execution_time"])[:10], "chore_id": to_int(c["chore_id"]), "name": c["chore_name"]}
                             for c in await self.g.get("/chores") if c.get("next_estimated_execution_time") and str(c["next_estimated_execution_time"])[:10] <= end]
        if self.cat.feature("FEATURE_FLAG_BATTERIES"):
            out["battery_charges"] = [{"day": str(b["next_estimated_charge_time"])[:10], "name": (b.get("battery") or {}).get("name")}
                                      for b in await self.g.get("/batteries") if inside(b.get("next_estimated_charge_time"))]
        return out

    async def r_get_spending(self, inp: dict) -> Any:
        end = parse_date(inp.get("to_date"), "to_date") or self.today().isoformat()
        start = parse_date(inp.get("from_date"), "from_date") or (self.today() - timedelta(days=30)).isoformat()
        rows = await self.g.objects("stock_log", ["transaction_type=purchase", "undone=0", f"purchased_date>={start}", f"purchased_date<={end}"])
        total, unpriced = 0.0, 0
        stores: dict[str, float] = {}
        groups: dict[str, float] = {}
        products: dict[str, float] = {}
        for r in rows:
            cost = to_float(r.get("price")) * to_float(r.get("amount"))
            if cost <= 0:
                unpriced += 1
                continue
            total += cost
            p = self.cat.product(r["product_id"]) or {}
            store = self.cat.stores.get(to_int(r.get("shopping_location_id")) or -1, {}).get("name", "No store given")
            group = self.cat.groups.get(to_int(p.get("product_group_id")) or -1, {}).get("name", "No group")
            name = self.cat.product_name(r["product_id"])
            stores[store] = stores.get(store, 0) + cost
            groups[group] = groups.get(group, 0) + cost
            products[name] = products.get(name, 0) + cost

        def top(data: dict[str, float], n: int = 50) -> dict[str, str]:
            return {k: self.cat.money(v) for k, v in sorted(data.items(), key=lambda kv: -kv[1])[:n]}

        return {"from": start, "to": end, "total": self.cat.money(total), "by_store": top(stores), "by_product_group": top(groups),
                "biggest_items": top(products, 10), "purchases_without_a_price": unpriced}

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
            stock_entry = None
            if it.get("entry_id"):
                entry = await self.g.get(f"/stock/entry/{to_int(it['entry_id'])}")
                if not entry or to_int(entry.get("product_id")) != pid:
                    raise ToolError(f"Stock entry {it['entry_id']} does not belong to {p['name']}. Use an entry id from get_stock.")
                if amount > to_float(entry["amount"]):
                    raise ToolError(f"That stock entry only holds {self.cat.amount(pid, entry['amount'])}.")
                stock_entry = entry["stock_id"]
            out.append(action("consume", "remove", p["name"], detail, product_id=pid, amount=amount, spoiled=spoiled, location_id=loc, stock_entry_id=stock_entry))
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

    def need_list(self, list_id: Any) -> int:
        lid = to_int(list_id) or 1
        if self.cat.lists and lid not in self.cat.lists:
            raise ToolError(f"There is no shopping list with id {list_id}.")
        return lid

    def list_suffix(self, list_id: int) -> str:
        """Name the list on the card when the household has more than one."""
        return f" · {self.cat.lists[list_id]['name']}" if len(self.cat.lists) > 1 and list_id in self.cat.lists else ""

    async def p_add_to_shopping_list(self, inp: dict) -> list[dict]:
        list_id = self.need_list(inp.get("list_id"))
        where = self.list_suffix(list_id)
        out = []
        for it in self.items(inp):
            amount = self.need_amount(it.get("amount", 1))
            note = (it.get("note") or "").strip() or None
            if it.get("product_id"):
                p = await self.need_product(it["product_id"])
                detail = self.cat.amount(p["id"], amount) + (f" · {note}" if note else "") + where
                out.append(action("list_add", "list", p["name"], detail, product_id=int(p["id"]), amount=amount, note=note, list_id=list_id))
            elif note:
                out.append(action("list_add", "list", note, ((fmt_num(amount) if amount != 1 else "") + where).removeprefix(" · "), product_id=None, amount=amount, note=note, list_id=list_id))
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
        if inp.get("list_id"):
            body["shopping_list_id"] = self.need_list(inp["list_id"])
            changes.append(f"moved to {self.cat.lists[body['shopping_list_id']]['name']}")
        if not body:
            raise ToolError("No changes were given.")
        return [action("list_update", "list", name, cap(", ".join(changes)), item_id=int(row["id"]), body=body)]

    async def p_add_low_stock_to_shopping_list(self, inp: dict) -> list[dict]:
        v = await self.g.get("/stock/volatile")
        overdue, expired = bool(inp.get("also_overdue")), bool(inp.get("also_expired"))
        names = [m["name"] for m in v.get("missing_products") or []]
        if overdue:
            names += [self.cat.product_name(r["product_id"]) for r in v.get("overdue_products") or []]
        if expired:
            names += [self.cat.product_name(r["product_id"]) for r in v.get("expired_products") or []]
        if not names:
            raise ToolError("Nothing is below its minimum stock" + (" or past its date" if overdue or expired else "") + "; nothing to add.")
        list_id = self.need_list(inp.get("list_id"))
        return [action("list_add_missing", "list", "Products to restock", ", ".join(dict.fromkeys(names[:15])) + self.list_suffix(list_id),
                       list_id=list_id, overdue=overdue, expired=expired)]

    async def p_clear_shopping_list(self, inp: dict) -> list[dict]:
        list_id = self.need_list(inp.get("list_id"))
        done_only = bool(inp.get("done_only"))
        rows = await self.g.objects("shopping_list", [f"shopping_list_id={list_id}"])
        count = len([r for r in rows if to_int(r.get("done"))] if done_only else rows)
        if not count:
            raise ToolError("There is nothing to remove from that list.")
        what = "ticked-off items" if done_only else "items"
        return [action("list_clear", "remove", self.cat.lists.get(list_id, {}).get("name") or "Shopping list", f"Remove {'the' if done_only else 'all'} {count} {what}", list_id=list_id, done_only=done_only)]

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
        for key, label in (("default_best_before_days", "shelf life"), ("default_best_before_days_after_open", "shelf life after opening"),
                           ("default_best_before_days_after_freezing", "shelf life after freezing"), ("default_best_before_days_after_thawing", "shelf life after thawing")):
            if inp.get(key) is not None:
                fields[key] = to_int(inp[key]) or 0
                changes.append(f"{label} {'never expires' if fields[key] == -1 else str(fields[key]) + ' days'}")
        if inp.get("due_type") in ("best_before", "expiration"):
            fields["due_type"] = 2 if inp["due_type"] == "expiration" else 1
            changes.append("date is a hard expiry date" if fields["due_type"] == 2 else "date is a best-before date")
        if inp.get("parent_product_id") is not None:
            parent = to_int(inp["parent_product_id"])
            if parent:
                if parent == int(p["id"]):
                    raise ToolError("A product cannot be its own parent.")
                fields["parent_product_id"] = int((await self.need_product(parent))["id"])
                changes.append(f"variant of {self.cat.product_name(parent)}")
            else:
                fields["parent_product_id"] = None
                changes.append("no longer a variant of another product")
        if inp.get("active") is not None:
            fields["active"] = 1 if inp["active"] else 0
            changes.append("in use again" if inp["active"] else "hidden as no longer used")
        for key, label in (("quick_consume_amount", "quick-use amount"), ("calories", "calories per unit")):
            if inp.get(key) is not None:
                fields[key] = to_float(inp[key])
                changes.append(f"{label} {fmt_num(fields[key])}")
        for key, yes, no in (("treat_opened_as_out_of_stock", "opened packs count as used up", "opened packs still count as stock"),
                             ("should_not_be_frozen", "should not be frozen", "may be frozen"),
                             ("hide_on_stock_overview", "hidden from Grocy's stock overview", "shown on Grocy's stock overview")):
            if inp.get(key) is not None:
                fields[key] = 1 if inp[key] else 0
                changes.append(yes if inp[key] else no)
        if not fields:
            raise ToolError("No changes were given.")
        return [action("update_product", "edit", p["name"], cap("; ".join(changes)), product_id=int(p["id"]), fields=fields)]

    def master_fields(self, kind: str, inp: dict) -> tuple[dict, list[str]]:
        body: dict[str, Any] = {}
        changes: list[str] = []
        if inp.get("name_plural") and kind == "quantity_unit":
            body["name_plural"] = str(inp["name_plural"]).strip()
            changes.append(f"plural {body['name_plural']}")
        if inp.get("is_freezer") is not None and kind == "location":
            body["is_freezer"] = 1 if inp["is_freezer"] else 0
            changes.append("a freezer" if inp["is_freezer"] else "not a freezer")
        if inp.get("description") is not None and kind != "meal_plan_section":
            body["description"] = str(inp["description"])
            changes.append("description")
        if inp.get("charge_interval_days") is not None and kind == "battery":
            body["charge_interval_days"] = to_int(inp["charge_interval_days"]) or 0
            changes.append(f"charge every {body['charge_interval_days']} days")
        return body, changes

    async def master_clash(self, entity: str, name: str, own_id: int = 0) -> None:
        for r in await self.g.objects(entity):
            if str(r.get("name") or "").strip().lower() == name.lower() and to_int(r["id"]) != own_id:
                raise ToolError(f"{name!r} already exists with id {r['id']}.")

    async def p_create_master_item(self, inp: dict) -> list[dict]:
        kind, name = str(inp.get("kind")), str(inp.get("name", "")).strip()
        if kind not in MASTER or not name:
            raise ToolError("kind and name are required.")
        entity, word = MASTER[kind]
        await self.master_clash(entity, name)
        body, _ = self.master_fields(kind, inp)
        if kind == "quantity_unit":
            body.setdefault("name_plural", name)
        return [action("create_master", "new", name, f"New {word}", entity=entity, body={"name": name, **body})]

    async def p_update_master_item(self, inp: dict) -> list[dict]:
        kind = str(inp.get("kind"))
        if kind not in MASTER:
            raise ToolError("Unknown kind.")
        entity, word = MASTER[kind]
        row = await self.need_row(entity, inp.get("id"), word)
        body, changes = self.master_fields(kind, inp)
        name = str(inp.get("name") or "").strip()
        if name and name != row.get("name"):
            await self.master_clash(entity, name, to_int(row["id"]))
            body["name"] = name
            changes.insert(0, f"rename to {name}")
        if not body:
            raise ToolError("No changes were given.")
        return [action("update_master", "edit", row.get("name") or word, f"{word.capitalize()}: " + ", ".join(changes), entity=entity, row_id=to_int(row["id"]), body=body)]

    async def p_add_product_barcode(self, inp: dict) -> list[dict]:
        p = await self.need_product(inp.get("product_id"))
        code = re.sub(r"\s", "", str(inp.get("barcode") or ""))
        if len(code) < 4:
            raise ToolError("That does not look like a barcode.")
        taken = await self.g.objects("product_barcodes", [f"barcode={code}"])
        if taken:
            raise ToolError(f"That barcode already belongs to {self.cat.product_name(taken[0]['product_id'])}.")
        body: dict[str, Any] = {"product_id": int(p["id"]), "barcode": code}
        detail = f"Barcode {code}"
        if inp.get("amount") is not None:
            body["amount"] = self.need_amount(inp["amount"])
            detail += f" · one scan = {self.cat.amount(p['id'], body['amount'])}"
        if inp.get("store_id"):
            if to_int(inp["store_id"]) not in self.cat.stores:
                raise ToolError("Unknown store.")
            body["shopping_location_id"] = to_int(inp["store_id"])
        if inp.get("note"):
            body["note"] = str(inp["note"])
        return [action("create_row", "new", p["name"], detail, entity="product_barcodes", body=body, summary=f"Barcode {code} attached to {p['name']}")]

    async def p_add_unit_conversion(self, inp: dict) -> list[dict]:
        src, dst, factor = to_int(inp.get("from_unit_id")), to_int(inp.get("to_unit_id")), to_float(inp.get("factor"))
        if src not in self.cat.units or dst not in self.cat.units or src == dst:
            raise ToolError("from_unit_id and to_unit_id must be two different existing quantity units.")
        if factor <= 0:
            raise ToolError("factor must be greater than 0.")
        body: dict[str, Any] = {"from_qu_id": src, "to_qu_id": dst, "factor": factor}
        title = "Unit conversion"
        query = [f"from_qu_id={src}", f"to_qu_id={dst}"]
        if inp.get("product_id"):
            p = await self.need_product(inp["product_id"])
            body["product_id"], title = int(p["id"]), p["name"]
            query.append(f"product_id={p['id']}")
        existing = [r for r in await self.g.objects("quantity_unit_conversions", query) if bool(r.get("product_id")) == bool(body.get("product_id"))]
        if existing:
            raise ToolError(f"That conversion already exists (1 {self.cat.unit_name(src)} = {fmt_num(existing[0]['factor'])} {self.cat.unit_name(dst, 2)}).")
        text = f"1 {self.cat.unit_name(src)} = {fmt_num(factor)} {self.cat.unit_name(dst, factor)}"
        return [action("create_row", "new", title, text, entity="quantity_unit_conversions", body=body, summary=f"Saved: {text}")]

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
        if not positions:
            raise ToolError("That recipe has no ingredients, so there is nothing to take out of stock.")
        names = ", ".join(self.cat.product_name(p["product_id"]) for p in positions[:12])
        servings = to_float(inp.get("servings")) or None
        if servings is not None and servings <= 0:
            raise ToolError("servings must be greater than 0.")
        portion = f" for {fmt_num(servings)} servings" if servings else ""
        return [action("recipe_consume", "remove", recipe["name"], f"Use up its ingredients{portion}: {names}", recipe_id=int(recipe["id"]), servings=servings)]

    async def p_add_recipe_missing_to_shopping_list(self, inp: dict) -> list[dict]:
        recipe = await self.recipe(inp.get("recipe_id"))
        return [action("recipe_to_list", "list", recipe["name"], "Missing ingredients added to the list", recipe_id=int(recipe["id"]))]

    async def ingredients(self, raw: Any) -> list[dict]:
        out = []
        for ing in raw or []:
            if not isinstance(ing, dict):
                raise ToolError("ingredients must be a list of objects.")
            p = await self.need_product(ing.get("product_id"))
            row = {"product_id": int(p["id"]), "amount": self.need_amount(ing.get("amount")), "qu_id": to_int(p.get("qu_id_stock")),
                   "note": ing.get("note") or None, "variable_amount": ing.get("variable_amount") or None,
                   "ingredient_group": ing.get("group") or None}
            if ing.get("only_check_any_in_stock") is not None:
                row["only_check_single_unit_in_stock"] = 1 if ing["only_check_any_in_stock"] else 0
            out.append(row)
        return out

    def ingredient_text(self, ingredients: list[dict]) -> str:
        return ", ".join(f"{i.get('variable_amount') or self.cat.amount(i['product_id'], i['amount'])} {self.cat.product_name(i['product_id'])}" for i in ingredients[:14])

    async def p_create_recipe(self, inp: dict) -> list[dict]:
        name = str(inp.get("name", "")).strip()
        if not name:
            raise ToolError("The recipe needs a name.")
        if any(str(r["name"]).strip().lower() == name.lower() for r in await self.g.objects("recipes", ["type=normal"])):
            raise ToolError(f"A recipe named {name!r} already exists. Use update_recipe to change it.")
        ingredients = await self.ingredients(inp.get("ingredients"))
        if not ingredients:
            raise ToolError("The recipe needs at least one ingredient.")
        return [action("create_recipe", "new", name, self.ingredient_text(ingredients), name=name, instructions=str(inp.get("instructions") or ""), servings=to_int(inp.get("servings")) or 1, ingredients=ingredients)]

    async def p_update_recipe(self, inp: dict) -> list[dict]:
        recipe = await self.recipe(inp.get("recipe_id"))
        fields, changes = {}, []
        if inp.get("name") and str(inp["name"]).strip() != recipe["name"]:
            fields["name"] = str(inp["name"]).strip()
            changes.append(f"rename to {fields['name']}")
        if inp.get("instructions") is not None:
            fields["description"] = recipe_html(str(inp["instructions"]))
            changes.append("new instructions")
        if inp.get("servings"):
            fields["base_servings"] = to_int(inp["servings"])
            changes.append(f"{fields['base_servings']} servings")
        # Each given ingredient either changes the row for that product or is added as a new row.
        current = {to_int(r["product_id"]): to_int(r["id"]) for r in await self.g.objects("recipes_pos", [f"recipe_id={recipe['id']}"])}
        changed, added = [], []
        for ing in await self.ingredients(inp.get("ingredients")):
            if ing["product_id"] in current:
                changed.append({"row_id": current[ing["product_id"]], **ing})
            else:
                added.append(ing)
        if changed:
            changes.append("change " + self.ingredient_text(changed))
        if added:
            changes.append("add " + self.ingredient_text(added))
        if not changes:
            raise ToolError("No changes were given.")
        return [action("update_recipe", "edit", recipe["name"], cap("; ".join(changes)), recipe_id=int(recipe["id"]), fields=fields, changed=changed, added=added)]

    async def need_section(self, section_id: Any) -> tuple[int, str]:
        sections = await self.sections()
        if to_int(section_id) not in sections:
            raise ToolError(f"There is no meal plan section with id {section_id}. Known: {sections}")
        return to_int(section_id), sections[to_int(section_id)]

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
        detail = f"Planned for {nice_date(day)}"
        if inp.get("section_id"):
            body["section_id"], section = await self.need_section(inp["section_id"])
            detail += f" · {section}"
        return [action("meal_plan_add", "list", title, detail, body={**body, "day": day})]

    async def p_update_meal_plan_entry(self, inp: dict) -> list[dict]:
        row = await self.need_row("meal_plan", inp.get("entry_id"), "meal plan entry")
        body, changes = {}, []
        if inp.get("day"):
            body["day"] = parse_date(inp["day"], "day")
            changes.append(f"moved to {nice_date(body['day'])}")
        if inp.get("servings") is not None:
            amount = self.need_amount(inp["servings"], "servings")
            body["product_amount" if row.get("type") == "product" else "recipe_servings"] = amount
            changes.append(f"{fmt_num(amount)} servings")
        if inp.get("section_id"):
            body["section_id"], section = await self.need_section(inp["section_id"])
            changes.append(section)
        if inp.get("note") is not None:
            if row.get("type") != "note":
                raise ToolError("Only free-text entries have a note.")
            body["note"] = str(inp["note"])
            changes.append(f"now: {body['note']}")
        if inp.get("done") is not None:
            body["done"] = 1 if inp["done"] else 0
            changes.append("done" if inp["done"] else "not done")
        if not body:
            raise ToolError("No changes were given.")
        return [action("update_row", "list", await self.meal_title(row), cap(", ".join(changes)), entity="meal_plan", row_id=to_int(row["id"]), body=body, summary="Meal plan entry updated")]

    async def chore(self, chore_id: Any) -> dict:
        return await self.need_row("chores", chore_id, "chore")

    async def p_track_chore(self, inp: dict) -> list[dict]:
        chore = await self.chore(inp.get("chore_id"))
        body: dict[str, Any] = {}
        detail = "Marked as done"
        if inp.get("skipped"):
            body["skipped"], detail = True, "Skipped this time"
        when = parse_date(inp.get("done_on"), "done_on")
        if when:
            body["tracked_time"] = f"{when} 12:00:00"
            detail += f" on {nice_date(when)}"
        if inp.get("done_by_user_id"):
            body["done_by"], who = await self.need_user(inp["done_by_user_id"])
            detail += f" by {who}"
        return [action("chore_track", "list", chore["name"], detail, chore_id=int(chore["id"]), body=body)]

    async def p_undo_chore(self, inp: dict) -> list[dict]:
        row = await self.need_row("chores_log", inp.get("execution_id"), "chore execution")
        if to_int(row.get("undone")):
            raise ToolError("That execution was already undone.")
        chore = await self.chore(row["chore_id"])
        return [action("chore_undo", "undo", chore["name"], f"Undo: done {str(row.get('tracked_time'))[:10]}", execution_id=to_int(row["id"]))]

    async def chore_fields(self, inp: dict) -> tuple[dict, list[str]]:
        body: dict[str, Any] = {}
        changes: list[str] = []
        if inp.get("description") is not None:
            body["description"] = str(inp["description"])
            changes.append("description")
        schedule = inp.get("schedule")
        if schedule is not None:
            if schedule not in ("manually", "daily", "weekly", "monthly", "yearly"):
                raise ToolError("schedule must be manually, daily, weekly, monthly or yearly.")
            body["period_type"] = schedule
        if inp.get("every") is not None:
            body["period_interval"] = max(1, to_int(inp["every"]) or 1)
        if inp.get("weekdays") is not None:
            days = [str(x).lower() for x in inp["weekdays"]]
            if not days or any(x not in WEEKDAYS for x in days):
                raise ToolError("weekdays must be English weekday names.")
            body["period_config"] = ",".join(x for x in WEEKDAYS if x in days)
        if inp.get("day_of_month") is not None:
            if not 1 <= (to_int(inp["day_of_month"]) or 0) <= 31:
                raise ToolError("day_of_month must be between 1 and 31.")
            body["period_days"] = to_int(inp["day_of_month"])
        if schedule == "weekly" and "period_config" not in body:
            raise ToolError("A weekly chore needs weekdays.")
        if schedule == "monthly" and "period_days" not in body:
            raise ToolError("A monthly chore needs day_of_month.")
        if body.keys() & {"period_type", "period_interval", "period_config", "period_days"}:
            changes.append(self.chore_schedule(body) if schedule else "schedule details")
        if inp.get("start_date"):
            body["start_date"] = parse_date(inp["start_date"], "start_date") + " 00:00:00"
            changes.append(f"starting {nice_date(body['start_date'][:10])}")
        if inp.get("assignment") is not None:
            if inp["assignment"] not in ("no-assignment", "who-least-did-first", "random", "in-alphabetical-order"):
                raise ToolError("Unknown assignment.")
            body["assignment_type"] = inp["assignment"]
            changes.append({"no-assignment": "nobody assigned", "who-least-did-first": "whoever did it least goes next", "random": "random person", "in-alphabetical-order": "taking turns"}[inp["assignment"]])
        if inp.get("assigned_user_ids") is not None:
            people = [await self.need_user(u) for u in inp["assigned_user_ids"]]
            body["assignment_config"] = ",".join(str(i) for i, _ in people)
            changes.append("for " + ", ".join(n for _, n in people))
        return body, changes

    async def p_create_chore(self, inp: dict) -> list[dict]:
        name = str(inp.get("name", "")).strip()
        if not name:
            raise ToolError("The chore needs a name.")
        await self.master_clash("chores", name)
        body, changes = await self.chore_fields(inp)
        if "period_type" not in body:
            raise ToolError("schedule is required.")
        body = {"name": name, "period_interval": 1, "track_date_only": 1, "assignment_type": "no-assignment", **body}
        if body.get("assignment_config") and body["assignment_type"] == "no-assignment":
            body["assignment_type"] = "who-least-did-first"
        return [action("create_row", "new", name, "New chore: " + ", ".join(c for c in changes if c != "description"), entity="chores", body=body, summary=f"Chore {name} created")]

    async def p_update_chore(self, inp: dict) -> list[dict]:
        chore = await self.chore(inp.get("chore_id"))
        body, changes = await self.chore_fields(inp)
        name = str(inp.get("name") or "").strip()
        if name and name != chore["name"]:
            await self.master_clash("chores", name, int(chore["id"]))
            body["name"] = name
            changes.insert(0, f"rename to {name}")
        if inp.get("active") is not None:
            body["active"] = 1 if inp["active"] else 0
            changes.append("active again" if inp["active"] else "retired")
        if not body:
            raise ToolError("No changes were given.")
        return [action("update_row", "edit", chore["name"], cap(", ".join(changes)), entity="chores", row_id=int(chore["id"]), body=body, summary=f"Chore {chore['name']} updated")]

    async def task_extras(self, inp: dict) -> tuple[dict, list[str]]:
        body: dict[str, Any] = {}
        changes: list[str] = []
        if inp.get("category_id"):
            category = await self.need_row("task_categories", inp["category_id"], "task category")
            body["category_id"] = to_int(category["id"])
            changes.append(category["name"])
        if inp.get("assigned_to_user_id"):
            body["assigned_to_user_id"], who = await self.need_user(inp["assigned_to_user_id"])
            changes.append(f"for {who}")
        return body, changes

    async def p_add_task(self, inp: dict) -> list[dict]:
        name = str(inp.get("name", "")).strip()
        if not name:
            raise ToolError("The task needs a name.")
        due = parse_date(inp.get("due_date"), "due_date")
        extra, notes = await self.task_extras(inp)
        body = {"name": name, "description": str(inp.get("description") or ""), **({"due_date": due} if due else {}), **extra}
        return [action("task_add", "list", name, " · ".join([f"Task added{', due ' + nice_date(due) if due else ''}", *notes]), body=body)]

    async def p_update_task(self, inp: dict) -> list[dict]:
        task = await self.need_row("tasks", inp.get("task_id"), "task")
        body, changes = await self.task_extras(inp)
        if inp.get("name") and str(inp["name"]).strip() != task["name"]:
            body["name"] = str(inp["name"]).strip()
            changes.insert(0, f"renamed to {body['name']}")
        if inp.get("due_date") is not None:
            body["due_date"] = parse_date(inp["due_date"], "due_date")
            changes.append(f"due {nice_date(body['due_date'])}" if body["due_date"] else "no due date")
        if inp.get("description") is not None:
            body["description"] = str(inp["description"])
            changes.append("new description")
        if not body:
            raise ToolError("No changes were given.")
        return [action("update_row", "list", task["name"], cap(", ".join(changes)), entity="tasks", row_id=to_int(task["id"]), body=body, summary="Task updated")]

    async def p_complete_task(self, inp: dict) -> list[dict]:
        task = await self.need_row("tasks", inp.get("task_id"), "task")
        if to_int(task.get("done")):
            raise ToolError("That task is already completed.")
        return [action("task_complete", "list", task["name"], "Task completed", task_id=to_int(task["id"]))]

    async def p_reopen_task(self, inp: dict) -> list[dict]:
        task = await self.need_row("tasks", inp.get("task_id"), "task")
        if not to_int(task.get("done")):
            raise ToolError("That task is still open.")
        return [action("task_reopen", "undo", task["name"], "Open again", task_id=to_int(task["id"]))]

    async def p_charge_battery(self, inp: dict) -> list[dict]:
        battery = await self.need_row("batteries", inp.get("battery_id"), "battery")
        when = parse_date(inp.get("charged_on"), "charged_on")
        return [action("battery_charge", "list", battery["name"], f"Charged{' on ' + nice_date(when) if when else ''}", battery_id=to_int(battery["id"]), body={"tracked_time": f"{when} 12:00:00"} if when else {})]

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
        for key, value in (("price", price), ("location_id", location_id), ("shopping_location_id", store_id), ("purchased_date", purchased_date), ("note", signed(note))):
            if value is not None:
                body[key] = value
        rows = await self.g.post(f"/stock/products/{product_id}/add", body)
        try:
            await self.g.post("/stock/shoppinglist/remove-product", {"product_id": product_id, "product_amount": amount})
        except GrocyError:
            pass
        return self.booked(rows, f"Added {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)}", product_id=product_id, **extra)

    async def x_consume(self, product_id, amount, spoiled, location_id, stock_entry_id=None) -> dict:
        body = {"amount": amount, "transaction_type": "consume", "spoiled": spoiled}
        if location_id:
            body["location_id"] = location_id
        if stock_entry_id:
            body["stock_entry_id"] = stock_entry_id
        rows = await self.g.post(f"/stock/products/{product_id}/consume", body)
        left = await self.stock_amount(product_id)
        return self.booked(rows, f"Took {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)} out of stock; {self.cat.amount(product_id, left)} left", product_id=product_id)

    async def x_open(self, product_id, amount) -> dict:
        rows = await self.g.post(f"/stock/products/{product_id}/open", {"amount": amount})
        return self.booked(rows, f"Marked {self.cat.amount(product_id, amount)} {self.cat.product_name(product_id)} as opened")

    async def x_inventory(self, product_id, new_amount, best_before_date, location_id) -> dict:
        body = {"new_amount": new_amount, "best_before_date": best_before_date}
        if signed(None):
            body["note"] = signed(None)
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

    async def x_list_add_missing(self, list_id, overdue=False, expired=False) -> dict:
        await self.g.post("/stock/shoppinglist/add-missing-products", {"list_id": list_id})
        if overdue:
            await self.g.post("/stock/shoppinglist/add-overdue-products", {"list_id": list_id})
        if expired:
            await self.g.post("/stock/shoppinglist/add-expired-products", {"list_id": list_id})
        return {"summary": "Those products are on the shopping list"}

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

    async def x_update_master(self, entity, row_id, body) -> dict:
        await self.g.put(f"/objects/{entity}/{row_id}", body)
        await self.cat.refresh(force=True)
        return {"summary": "Updated"}

    # Generic create and edit of one row; the plan step has already validated entity, id and body.
    async def x_create_row(self, entity, body, summary) -> dict:
        new_id = await self.g.create(entity, body)
        return {"summary": summary, "created_id": new_id}

    async def x_update_row(self, entity, row_id, body, summary) -> dict:
        await self.g.put(f"/objects/{entity}/{row_id}", body)
        return {"summary": summary}

    async def x_update_recipe(self, recipe_id, fields, changed, added) -> dict:
        if fields:
            await self.g.put(f"/objects/recipes/{recipe_id}", fields)
        for ing in changed:
            await self.g.put(f"/objects/recipes_pos/{ing['row_id']}", {k: v for k, v in ing.items() if k != "row_id" and v is not None})
        for ing in added:
            await self.g.create("recipes_pos", {"recipe_id": recipe_id, **{k: v for k, v in ing.items() if v is not None}})
        return {"summary": "Recipe updated"}

    async def x_recipe_consume(self, recipe_id, servings=None) -> dict:
        if servings:
            await self.g.put(f"/objects/recipes/{recipe_id}", {"desired_servings": servings})
        await self.g.post(f"/recipes/{recipe_id}/consume")
        return {"summary": "Recipe ingredients taken out of stock"}

    async def x_recipe_to_list(self, recipe_id) -> dict:
        await self.g.post(f"/recipes/{recipe_id}/add-not-fulfilled-products-to-shoppinglist")
        return {"summary": "Missing ingredients are on the shopping list"}

    async def x_create_recipe(self, name, instructions, servings, ingredients) -> dict:
        rid = await self.g.create("recipes", {"name": name, "description": recipe_html(instructions), "base_servings": servings, "desired_servings": servings})
        for ing in ingredients:
            await self.g.create("recipes_pos", {"recipe_id": rid, **{k: v for k, v in ing.items() if v is not None}})
        return {"summary": f"Saved recipe {name}", "created_recipe_id": rid}

    async def x_meal_plan_add(self, body) -> dict:
        await self.g.create("meal_plan", body)
        return {"summary": f"Meal planned for {body['day']}"}

    async def x_chore_track(self, chore_id, body=None) -> dict:
        row = await self.g.post(f"/chores/{chore_id}/execute", body or {})
        execution = row.get("id") if isinstance(row, dict) else None
        return {"summary": "Chore recorded", **({"execution_id": execution} if execution else {})}

    async def x_chore_undo(self, execution_id) -> dict:
        await self.g.post(f"/chores/executions/{execution_id}/undo")
        return {"summary": "Chore execution undone"}

    async def x_task_reopen(self, task_id) -> dict:
        await self.g.post(f"/tasks/{task_id}/undo")
        return {"summary": "Task is open again"}

    async def x_battery_charge(self, battery_id, body) -> dict:
        await self.g.post(f"/batteries/{battery_id}/charge", body)
        return {"summary": "Battery charge recorded"}

    async def x_task_add(self, body) -> dict:
        tid = await self.g.create("tasks", body)
        return {"summary": f"Task added: {body['name']}", "created_task_id": tid}

    async def x_task_complete(self, task_id) -> dict:
        await self.g.post(f"/tasks/{task_id}/complete")
        return {"summary": "Task completed"}
