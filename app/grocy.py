"""Thin async client for Grocy's REST API plus a cached view of its master data."""
from __future__ import annotations

import time
from typing import Any

import httpx


class GrocyError(Exception):
    pass


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def fmt_num(value: Any) -> str:
    n = round(to_float(value), 3)
    return str(int(n)) if n == int(n) else f"{n:g}"


class Grocy:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api",
            headers={"GROCY-API-KEY": api_key, "Accept": "application/json"},
            timeout=20,
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        try:
            r = await self.http.request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            raise GrocyError(f"Could not reach Grocy ({type(e).__name__})") from e
        if r.status_code >= 400:
            message = ""
            try:
                message = r.json().get("error_message", "")
            except Exception:
                pass
            raise GrocyError(message or f"Grocy returned HTTP {r.status_code}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self.request("POST", path, json=body or {})

    async def put(self, path: str, body: dict) -> Any:
        return await self.request("PUT", path, json=body)

    async def objects(
        self, entity: str, query: list[str] | None = None, order: str | None = None, limit: int | None = None
    ) -> list[dict]:
        params: list[tuple[str, Any]] = [("query[]", q) for q in (query or [])]
        if order:
            params.append(("order", order))
        if limit:
            params.append(("limit", limit))
        return await self.request("GET", f"/objects/{entity}", params=params) or []

    async def create(self, entity: str, body: dict) -> int:
        res = await self.post(f"/objects/{entity}", body)
        return int(res["created_object_id"])


class Catalog:
    """Products, units, locations and stores, refreshed at most once a minute."""

    TTL = 60

    def __init__(self, grocy: Grocy):
        self.grocy = grocy
        self.loaded_at = 0.0
        self.products: dict[int, dict] = {}
        self.units: dict[int, dict] = {}
        self.locations: dict[int, dict] = {}
        self.stores: dict[int, dict] = {}
        self.groups: dict[int, dict] = {}
        self.lists: dict[int, dict] = {}
        self.config: dict = {}

    async def refresh(self, force: bool = False) -> None:
        if not force and self.loaded_at and time.monotonic() - self.loaded_at < self.TTL:
            return
        g = self.grocy

        def index(rows: list[dict]) -> dict[int, dict]:
            return {int(r["id"]): r for r in rows}

        self.products = index(await g.objects("products", order="name"))
        self.units = index(await g.objects("quantity_units"))
        self.locations = index(await g.objects("locations"))
        self.stores = index(await g.objects("shopping_locations"))
        self.groups = index(await g.objects("product_groups"))
        self.lists = index(await g.objects("shopping_lists"))
        if not self.config:
            self.config = await g.get("/system/config") or {}
        self.loaded_at = time.monotonic()

    def feature(self, flag: str) -> bool:
        return bool(self.config.get(flag, True))

    @property
    def currency(self) -> str:
        return str(self.config.get("CURRENCY") or "USD")

    def money(self, amount: float) -> str:
        symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "CAD": "$", "AUD": "$"}.get(self.currency)
        return f"{symbol}{amount:.2f}" if symbol else f"{amount:.2f} {self.currency}"

    def product(self, product_id: Any) -> dict | None:
        return self.products.get(to_int(product_id) or -1)

    def product_name(self, product_id: Any) -> str:
        p = self.product(product_id)
        return p["name"] if p else f"product #{product_id}"

    def location_name(self, location_id: Any) -> str:
        loc = self.locations.get(to_int(location_id) or -1)
        return loc["name"] if loc else ""

    def unit_name(self, qu_id: Any, amount: Any = 1) -> str:
        unit = self.units.get(to_int(qu_id) or -1)
        if not unit:
            return ""
        return unit["name"] if to_float(amount) == 1 else (unit.get("name_plural") or unit["name"])

    def amount(self, product_id: Any, amount: Any) -> str:
        """'2 bottles' for a product, using its stock unit."""
        p = self.product(product_id)
        unit = self.unit_name(p.get("qu_id_stock"), amount) if p else ""
        return f"{fmt_num(amount)} {unit}".strip()

    def text(self) -> str:
        """Compact listing that goes into the system prompt."""

        def names(rows: dict[int, dict]) -> str:
            return ", ".join(f"{i}={r['name']}" for i, r in sorted(rows.items())) or "(none)"

        lines = [
            "Locations (id=name): " + names(self.locations),
            "Quantity units (id=name): " + names(self.units),
            "Stores (id=name): " + names(self.stores),
            "Product groups (id=name): " + names(self.groups),
            "Shopping lists (id=name): " + names(self.lists),
            f"Currency: {self.currency}",
            "",
            "Products (id | name | stock unit | default location | group):",
        ]
        active = [p for p in self.products.values() if to_int(p.get("active", 1)) != 0]
        for p in active[:1500]:
            group = self.groups.get(to_int(p.get("product_group_id")) or -1, {}).get("name", "")
            lines.append(
                f"{p['id']} | {p['name']} | {self.unit_name(p.get('qu_id_stock'))} | "
                f"{self.location_name(p.get('location_id'))} | {group}"
            )
        if not active:
            lines.append("(no products yet)")
        if len(active) > 1500:
            lines.append(f"(+{len(active) - 1500} more; use search_products)")
        return "\n".join(lines)
