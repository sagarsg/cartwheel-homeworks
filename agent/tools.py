"""Homework 1: the remaining commerce-agent tools.

The three lecture tools (`search_help_center`, `get_order`, `issue_refund`)
are implemented in agent/agent.py and are worked examples of the pattern:
check permissions first, go through agent/db.py for data, and return a
structured dict, never a prose error. The homework tools follow the same
pattern. agent/agent.py already wraps each function below as an SDK tool, so
once a function works here it works in chat with no further wiring.

Result convention (see agent/auth.py):
  - Success: a dict with "ok": True plus the payload fields named in each
    docstring.
  - Failure: {"ok": False, "error": <code>, "reason": <human-readable str>}.

Run the contract tests with: uv run pytest tests/test_hw_holes.py -k hw1
They are marked xfail and flip to passing as you implement each function.
"""

from __future__ import annotations

import re
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any

from agent import db
from agent.auth import AuthContext, can_cancel_order, can_view_order, permission_denied
from agent.config import load_facts
from agent.helpcenter import load_policy_docs
from agent.killswitch import kill_switch
from seed.eligibility import effective_return_window_days

MAX_SEARCH_LIMIT = 25
DEFAULT_ORDER_LIMIT = 20
FIND_ORDER_LIMIT = 5

# find_order matching: words in a query that describe the purchase rather than
# the product ("the earmuffs I bought last week") are ignored.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_QUERY_STOPWORDS = frozenset(
    "a an and the my me i im for of to in on at from with this that it is "
    "was were be our your order orders ordered bought buy purchased got "
    "last week weeks month months year ago yesterday today recently recent "
    "some one any please can you find where".split()
)
_TOKEN_SIMILARITY = 0.85


def get_policy(ctx: AuthContext, policy_id: str) -> dict[str, Any]:
    """Fetch one policy doc by its exact id. Risk tier: read.

    Every role may read every policy doc (the corpus is public help-center
    content), so this tool needs no permission check.

    Args:
        ctx: The caller's auth context. Unused here, but every tool takes it.
        policy_id: An exact policy id, e.g. "cw-returns" or
            "store-juniper-home-goods-policy". Matching is exact and
            case-sensitive; ids are the `policy_id` front-matter field of the
            files in data/policies/.

    Returns:
        On success: {"ok": True, "policy_id": str, "title": str,
        "audience": str, "body": str} where body is the markdown body of the
        doc without the front matter.
        If no doc has that id: {"ok": False, "error": "not_found",
        "reason": ...} naming the id that was requested.

    Implementation notes:
        agent.helpcenter.load_policy_docs() returns every parsed doc.
    """
    for doc in load_policy_docs():
        if doc.policy_id == policy_id:
            return {
                "ok": True,
                "policy_id": doc.policy_id,
                "title": doc.title,
                "audience": doc.audience,
                "body": doc.body,
            }
    return {"ok": False, "error": "not_found", "reason": f"no policy doc with id {policy_id!r}"}


def search_products(
    ctx: AuthContext,
    query: str,
    store: str | None = None,
    max_price_usd: float | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search the product catalog. Risk tier: read.

    Every role may search products. Matching is deterministic keyword
    matching, not semantic search: a product matches when every whitespace
    token of `query` appears case-insensitively as a substring of the
    product's title or description.

    Args:
        ctx: The caller's auth context.
        query: Free-text query. Must be non-empty after stripping whitespace;
            otherwise return {"ok": False, "error": "invalid_argument",
            "reason": ...}.
        store: Optional store filter. Matched with
            agent.db.get_store_by_name (case-insensitive name or slug). If
            given and no store matches, return {"ok": False, "error":
            "not_found", "reason": ...} naming the store string.
        max_price_usd: Optional inclusive price ceiling. If given and not
            strictly positive, return an "invalid_argument" error.
        limit: Maximum products to return. Clamp to the range
            [1, MAX_SEARCH_LIMIT]; do not error on out-of-range values.

    Returns:
        {"ok": True, "products": [...], "count": <len(products)>} where each
        product is {"product_id": int, "store_id": int, "title": str,
        "price_usd": float}. Sort matches by price_usd ascending, then by
        product_id ascending, and truncate to `limit`. No matches is still a
        success: {"ok": True, "products": [], "count": 0}.

    Implementation notes:
        agent.db.list_products(conn, store_id) gives the candidate set.
        Use `with db.connection() as conn:` to close the database automatically.
    """
    tokens = query.lower().split()
    if not tokens:
        return {"ok": False, "error": "invalid_argument", "reason": "query must not be empty"}
    if max_price_usd is not None and max_price_usd <= 0:
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "max_price_usd must be greater than 0",
        }
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    with db.connection() as conn:
        store_id = None
        if store is not None:
            found = db.get_store_by_name(conn, store)
            if found is None:
                return {"ok": False, "error": "not_found", "reason": f"no store named {store!r}"}
            store_id = found.id
        candidates = db.list_products(conn, store_id)
    matches = []
    for product in candidates:
        text = f"{product.title} {product.description}".lower()
        if not all(token in text for token in tokens):
            continue
        if max_price_usd is not None and product.price_usd > max_price_usd:
            continue
        matches.append(product)
    matches.sort(key=lambda p: (p.price_usd, p.id))
    products = [
        {
            "product_id": p.id,
            "store_id": p.store_id,
            "title": p.title,
            "price_usd": p.price_usd,
        }
        for p in matches[:limit]
    ]
    return {"ok": True, "products": products, "count": len(products)}


def list_my_orders(ctx: AuthContext) -> dict[str, Any]:
    """List recent orders in the caller's own scope. Risk tier: read.

    Role behavior, straight from the access matrix in SPEC.md:
        - shopper: the caller's own orders.
        - merchant: the caller's store's orders (ctx.store_id).
        - support: support staff have no orders of their own and look up
          specific orders with get_order instead, so return {"ok": False,
          "error": "invalid_argument", "reason": ...} saying exactly that.

    Returns:
        For shopper and merchant: {"ok": True, "orders": [...],
        "count": <len(orders)>} where each order is
        agent.db.Order.to_public_dict() and the list holds at most
        DEFAULT_ORDER_LIMIT orders, newest first (agent.db.list_orders_for_user
        and list_orders_for_store already sort and limit this way).

    Implementation notes:
        No permission check is needed beyond the role dispatch, because the
        scope is baked into which query you run. That is the point of the
        tool: the model cannot ask for someone else's orders through it.
    """
    if ctx.role == "support":
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "support staff have no orders of their own; look up a specific order with get_order",
        }
    with db.connection() as conn:
        if ctx.role == "shopper":
            orders = db.list_orders_for_user(conn, ctx.user_id, limit=DEFAULT_ORDER_LIMIT)
        else:
            orders = db.list_orders_for_store(conn, ctx.store_id, limit=DEFAULT_ORDER_LIMIT)
    return {
        "ok": True,
        "orders": [order.to_public_dict() for order in orders],
        "count": len(orders),
    }


def cancel_order(ctx: AuthContext, order_id: int, reason: str) -> dict[str, Any]:
    """Cancel an order. Risk tier: write.

    This is the homework's write tool, and it must enforce two independent
    rules in this order:

    1. The access matrix (scope): use agent.auth.can_cancel_order. Shoppers
       may cancel only their own orders, merchants only their own store's
       orders, support any order. On failure return
       agent.auth.permission_denied(...) with a reason naming the role and
       the order id. Scope is checked before the status rule so that an
       out-of-scope caller learns nothing about the order's state.
    2. The pre-shipment rule (facts.yaml `cancel_cutoff`): only orders whose
       status is exactly "placed" can be cancelled, for every role. If the
       order is in scope but its status is not "placed", return
       {"ok": False, "error": "not_eligible", "reason": ...} that names the
       current status and states that orders can be cancelled only before
       shipment.

    Args:
        ctx: The caller's auth context.
        order_id: The order to cancel.
        reason: Free-text reason from the user; not validated.

    Returns:
        If no order has this id: {"ok": False, "error": "not_found",
        "reason": ...}.
        On success: {"ok": True, "order_id": order_id, "status": "cancelled"}
        after persisting the new status with agent.db.set_order_status.

    Implementation notes:
        Fetch with agent.db.get_order. Note the argument order of
        can_cancel_order(ctx, order_user_id, order_store_id).

    The Module 4 kill switch is checked first (before the scope and
    status rules and before your code), so that a paused write tool touches
    nothing. It is provided; the default ("off") returns None and falls
    through to your implementation.
    """
    paused = kill_switch("cancel_order")
    if paused is not None:
        return {"ok": False, "error": "paused", "reason": paused}
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "error": "not_found", "reason": f"no order #{order_id}"}
        if not can_cancel_order(ctx, order.user_id, order.store_id):
            return permission_denied(
                f"role '{ctx.role}' (user {ctx.user_id}) may not cancel order #{order_id}"
            )
        if order.status != "placed":
            return {
                "ok": False,
                "error": "not_eligible",
                "reason": (
                    f"order #{order_id} is '{order.status}'; orders can be cancelled "
                    "only before shipment"
                ),
            }
        db.set_order_status(conn, order_id, "cancelled")
    return {"ok": True, "order_id": order_id, "status": "cancelled"}


def find_order(ctx: AuthContext, query: str) -> dict[str, Any]:
    """Search the caller's orders by product name. Risk tier: read.

    Takes a natural-language query (e.g., "earmuffs I bought last week")
    and searches the authenticated user's orders for products whose name
    matches. Use fuzzy string matching (e.g., thefuzz.fuzz.partial_ratio
    or case-insensitive substring matching) to find orders whose product name is close to the
    query.

    Access rules: a shopper searches only the shopper's own orders, a
    merchant searches orders from the merchant's store, and support staff
    can search any orders. Use agent.db.list_order_search_candidates with
    user_id=ctx.user_id for shoppers, store_id=ctx.store_id for merchants,
    or all_orders=True only for support. Derive the scope from ctx, never
    from the query; reject unsupported roles or missing required identity.
    Use agent.db.list_products to map product IDs to product titles.

    The helper returns the complete authorised scope, newest first with
    order ID descending as the tie-breaker. Match product names first,
    preserve that order, then return at most five matches. Do not search
    only the 20 most recent orders. Convert matches with to_public_dict().

    Args:
        ctx: The caller's auth context.
        query: A natural-language description of the product.

    Returns:
        {"ok": True, "orders": [...]} with a list of matching orders
        (at most 5), each as the dict returned by agent.db. If no orders
        match, return {"ok": True, "orders": []}.
    """
    if ctx.role == "shopper" and ctx.user_id is not None:
        scope: dict[str, Any] = {"user_id": ctx.user_id}
    elif ctx.role == "merchant" and ctx.store_id is not None:
        scope = {"store_id": ctx.store_id}
    elif ctx.role == "support":
        scope = {"all_orders": True}
    else:
        return permission_denied(f"role '{ctx.role}' may not search orders")

    query_tokens = [t for t in _tokens(query) if t not in _QUERY_STOPWORDS]
    if not query_tokens:
        return {"ok": True, "orders": []}

    with db.connection() as conn:
        candidates = db.list_order_search_candidates(conn, **scope)
        titles = {p.id: p.title for p in db.list_products(conn)}

    # Score each product by how many query words it matches, then keep only
    # the best-scoring products, so "wool socks" prefers "Wool Socks" over
    # "Wool Earmuffs" while a one-word query still matches loosely.
    scores = {pid: _match_score(query_tokens, title) for pid, title in titles.items()}
    best = max((scores.get(o.product_id, 0) for o in candidates), default=0)
    if best == 0:
        return {"ok": True, "orders": []}
    matches = [o for o in candidates if scores.get(o.product_id, 0) == best]
    return {"ok": True, "orders": [o.to_public_dict() for o in matches[:FIND_ORDER_LIMIT]]}


def check_return_eligibility(ctx: AuthContext, order_id: int) -> dict[str, Any]:
    """Explain whether an order can be returned or refunded, and why. Risk tier: read.

    Added beyond the five required tools (HW1 Part A). get_order returns only a
    refund_eligible flag, so the model has to work out the reason and the
    deadline itself, which is where it misquotes windows and store overrides.
    This tool computes them from the same facts the refund tool uses.

    Access follows the "view orders" row of the access matrix, checked before
    anything about the order is revealed.

    Returns:
        If no order has this id: {"ok": False, "error": "not_found", ...}.
        Out of scope: agent.auth.permission_denied(...).
        Otherwise {"ok": True, "order_id", "status", "store_name",
        "eligible": bool, "return_window_days": int,
        "window_source": "store_override" | "platform", "delivered_at",
        "return_deadline" (None when not delivered), "days_left" (None when
        not delivered), "reason": str}.
    """
    facts = load_facts()
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "error": "not_found", "reason": f"no order #{order_id}"}
        if not can_view_order(ctx, order.user_id, order.store_id):
            return permission_denied(
                f"role '{ctx.role}' (user {ctx.user_id}) may not view order #{order_id}"
            )
        store = db.get_store(conn, order.store_id)
        today = db.world_asof(conn)

    override = store.return_window_days_override if store else None
    window = effective_return_window_days(facts["return_window_days"], override)
    result: dict[str, Any] = {
        "ok": True,
        "order_id": order_id,
        "status": order.status,
        "store_name": store.name if store else None,
        "eligible": order.refund_eligible,
        "return_window_days": window,
        "window_source": "store_override" if override is not None else "platform",
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "return_deadline": None,
        "days_left": None,
    }
    if order.status != "delivered" or order.delivered_at is None:
        result["reason"] = (
            f"order is '{order.status}'; only delivered orders can be returned, "
            "and the window counts from the delivery date"
        )
        return result
    deadline = order.delivered_at + timedelta(days=window)
    result["return_deadline"] = deadline.isoformat()
    result["days_left"] = (deadline - today).days
    if order.refund_eligible:
        result["reason"] = f"delivered {order.delivered_at}; returnable until {deadline}"
    else:
        result["reason"] = f"the {window}-day return window ended on {deadline}"
    return result


def get_store_policy(ctx: AuthContext, store: str) -> dict[str, Any]:
    """Return a store's return-window policy by store name. Risk tier: read.

    Added beyond the five required tools (HW1 Part B). Answers store-level
    policy questions ("what is the return window for <store>?") that carry no
    order id, so the agent no longer has to hope help-center search ranking
    surfaces the right store doc. Reads the authoritative
    `return_window_days_override` column, not the policy prose, which also
    avoids the doc front-matter/prose mismatches.

    Store policy is public help-center content, so every role may read it and
    no permission check is needed.

    Args:
        ctx: The caller's auth context. Unused here, but every tool takes it.
        store: The store's display name, e.g. "Northwind Books".
            Case-insensitive; slug also accepted.

    Returns:
        On success: {"ok": True, "store_id": int, "store_name": str,
        "return_window_days": int, "window_source":
        "store_override" | "platform"}.
        If no store matches: {"ok": False, "error": "not_found",
        "reason": ...} naming the store that was requested.
    """
    facts = load_facts()
    with db.connection() as conn:
        store_row = db.get_store_by_name(conn, store)
    if store_row is None:
        return {"ok": False, "error": "not_found", "reason": f"no store named {store!r}"}
    override = store_row.return_window_days_override
    window = effective_return_window_days(facts["return_window_days"], override)
    return {
        "ok": True,
        "store_id": store_row.id,
        "store_name": store_row.name,
        "return_window_days": window,
        "window_source": "store_override" if override is not None else "platform",
    }


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _match_score(query_tokens: list[str], title: str) -> int:
    """Count query words that closely match a word of the product title."""
    title_tokens = _tokens(title)
    return sum(
        any(
            SequenceMatcher(None, q, t).ratio() >= _TOKEN_SIMILARITY
            for t in title_tokens
        )
        for q in query_tokens
    )
