"""Watchlist business logic."""

import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

from app.data.market_symbols_seed import get_symbol_name as seed_get_symbol_name
from app.services.market.symbol_search import find_market_symbol
from app.services.symbol_name import normalize_crypto_symbol, persist_seed_name, resolve_symbol_name
from app.services.market_context import MarketContext, SUPPORTED_CRYPTO_EXCHANGE_IDS
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

VALID_MARKETS = frozenset({
    "Crypto", "USStock", "CNStock", "HKStock", "Forex", "Futures", "MOEX",
})
CN_A_SHARE_PATTERN = re.compile(r"^\d{6}$")

_name_resolve_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="watchlist-name-resolve",
)


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def validate_watchlist_pair(market: str, symbol: str) -> Optional[str]:
    """Validate a market/symbol pair before persisting it."""
    if market not in VALID_MARKETS:
        return f"Unsupported market '{market}'. Must be one of: {', '.join(sorted(VALID_MARKETS))}"
    if not symbol:
        return "Empty symbol"
    if CN_A_SHARE_PATTERN.match(symbol) and market != "CNStock":
        return f"Symbol '{symbol}' looks like a Chinese A-share code; market must be CNStock, not {market}"
    if symbol.endswith(".HK") and market != "HKStock":
        return f"Symbol '{symbol}' looks like a Hong Kong stock; market must be HKStock, not {market}"
    if market == "Crypto" and "/" not in symbol:
        return f"Crypto symbol '{symbol}' must be a BASE/QUOTE pair (e.g. BTC/USDT)."
    return None


def list_watchlist(user_id: int) -> list:
    """Return watchlist rows with their exact market-data identity and group."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT w.id, w.market, w.symbol, w.name, w.exchange_id, w.market_type,
                   w.instrument_id, w.settle_currency,
                   w.group_id, w.sort_order,
                   COALESCE(g.name, '') AS group_name,
                   COALESCE(g.sort_order, 0) AS group_sort_order,
                   COALESCE(s.asset_class, '') AS asset_class,
                   COALESCE(s.product_type, '') AS product_type,
                   COALESCE(s.api_family, '') AS api_family,
                   COALESCE(s.underlying_market, '') AS underlying_market,
                   COALESCE(s.underlying_symbol, '') AS underlying_symbol,
                   COALESCE(s.product_meta, '{}'::jsonb) AS product_meta
            FROM qd_watchlist w
            LEFT JOIN qd_watchlist_groups g
              ON g.id = w.group_id AND g.user_id = w.user_id
            LEFT JOIN qd_market_symbols s
              ON s.market = w.market
             AND s.symbol = w.symbol
             AND s.exchange = w.exchange_id
             AND s.market_type = w.market_type
             AND s.instrument_id = w.instrument_id
            WHERE w.user_id = ?
            ORDER BY COALESCE(g.sort_order, 0), w.group_id NULLS LAST, w.sort_order, w.id DESC
            """,
            (user_id,),
        )
        stored_rows = cur.fetchall() or []
        rows = []
        for row in stored_rows:
            _backfill_row_name(cur, user_id, row)
            rows.append(row)
        db.commit()
        cur.close()
    return rows


def add_watchlist_item(
    user_id: int,
    market: str,
    raw_symbol: str,
    name_in: str = "",
    *,
    exchange_id: str = "",
    market_type: str = "",
    instrument_id: str = "",
    settle_currency: str = "",
    group_id: Optional[int] = None,
) -> tuple[bool, str]:
    """Validate and persist a watchlist item."""
    result = upsert_watchlist_item(
        user_id,
        market,
        raw_symbol,
        name_in,
        exchange_id=exchange_id,
        market_type=market_type,
        instrument_id=instrument_id,
        settle_currency=settle_currency,
        group_id=group_id,
    )
    if not result.get("ok"):
        return False, str(result.get("error") or "failed")
    return True, "success"


def upsert_watchlist_item(
    user_id: int,
    market: str,
    raw_symbol: str,
    name_in: str = "",
    *,
    exchange_id: str = "",
    market_type: str = "",
    instrument_id: str = "",
    settle_currency: str = "",
    group_id: Optional[int] = None,
) -> dict:
    """Validate and upsert one watchlist item; report added vs updated."""
    market = (market or "").strip()
    symbol = normalize_symbol(raw_symbol)
    if not market or not symbol:
        return {"ok": False, "error": "Missing market or symbol"}

    if market == "Crypto":
        symbol = normalize_crypto_symbol(symbol)
    context = MarketContext.from_mapping({
        "market": market,
        "symbol": symbol,
        "exchange_id": exchange_id,
        "market_type": market_type,
        "instrument_id": instrument_id,
        "settle_currency": settle_currency,
    })
    if market == "Crypto" and context.exchange_id not in SUPPORTED_CRYPTO_EXCHANGE_IDS:
        return {"ok": False, "error": f"Unsupported crypto exchange: {context.exchange_id}"}

    validation_err = validate_watchlist_pair(market, symbol)
    if validation_err:
        logger.info("Rejecting watchlist add for user %s: %s", user_id, validation_err)
        return {"ok": False, "error": validation_err}

    matched = find_market_symbol(
        market,
        symbol,
        exchange_id=context.exchange_id,
        market_type=context.market_type,
    )
    if not matched:
        err = (
            f"Symbol '{symbol}' not found on {market}. "
            "Please verify the ticker and market, or pick from search results."
        )
        logger.info("Rejecting watchlist add for user %s: %s", user_id, err)
        return {"ok": False, "error": err}
    matched_instrument_id = str(matched.get("instrument_id") or "").strip()
    if instrument_id and matched_instrument_id != str(instrument_id).strip():
        err = f"Instrument '{instrument_id}' does not match {context.exchange_id}:{context.market_type}:{symbol}."
        logger.info("Rejecting watchlist add for user %s: %s", user_id, err)
        return {"ok": False, "error": err}

    resolved = (
        (matched.get("name") or "").strip()
        or resolve_symbol_name_bounded(market, symbol)
        or seed_get_symbol_name(market, symbol)
    )
    name = (name_in or "").strip() or resolved or symbol
    persist_seed_name(market, symbol, name)
    settle_currency = str((matched or {}).get("settle_currency") or context.settle_currency or "").strip().upper()
    stored_exchange_id = str(matched.get("exchange_id") or context.exchange_id or "").strip().lower()
    stored_market_type = str(matched.get("market_type") or context.market_type or "spot").strip().lower()
    stored_instrument_id = matched_instrument_id or str(context.instrument_id or "").strip()

    owned_group_id = _resolve_owned_group_id(user_id, group_id)
    if group_id is not None and owned_group_id is None:
        return {"ok": False, "error": f"Group {group_id} not found"}

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_watchlist (
                user_id, market, symbol, name, exchange_id, market_type,
                instrument_id, settle_currency, group_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())
            ON CONFLICT(user_id, market, symbol, exchange_id, market_type, instrument_id) DO UPDATE SET
                name = excluded.name,
                settle_currency = excluded.settle_currency,
                group_id = COALESCE(excluded.group_id, qd_watchlist.group_id),
                updated_at = NOW()
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                user_id,
                market,
                symbol,
                name,
                stored_exchange_id,
                stored_market_type,
                stored_instrument_id,
                settle_currency,
                owned_group_id,
            ),
        )
        # RETURNING needs a fetch; test capture-cursors may not implement it.
        fetch = getattr(cur, "fetchone", None)
        row = fetch() if callable(fetch) else {}
        if row is None:
            row = {}
        db.commit()
        cur.close()
    return {
        "ok": True,
        "status": "added" if row.get("inserted") else "updated",
        "id": row.get("id"),
        "market": market,
        "symbol": symbol,
    }


def add_watchlist_items(
    user_id: int,
    items: list,
    group_id: Optional[int] = None,
) -> dict:
    """Batch add items; idempotent per item via the unique market context.

    Returns {added, updated, failed, errors, total}. Existing rows are
    updated (name refresh / group assignment), never duplicated.
    """
    added = updated = 0
    errors: list = []
    seen_keys: set = set()
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            errors.append({"index": index, "error": "Item must be an object"})
            continue
        market = str(item.get("market") or "").strip()
        symbol = normalize_symbol(item.get("symbol"))
        key = (market, symbol)
        if key in seen_keys:
            errors.append({"index": index, "market": market, "symbol": symbol, "error": "Duplicate item in request"})
            continue
        seen_keys.add(key)
        result = upsert_watchlist_item(
            user_id,
            market,
            symbol,
            str(item.get("name") or ""),
            exchange_id=str(item.get("exchange_id") or item.get("exchangeId") or "").strip(),
            market_type=str(item.get("market_type") or item.get("marketType") or "").strip(),
            instrument_id=str(item.get("instrument_id") or item.get("instrumentId") or "").strip(),
            settle_currency=str(item.get("settle_currency") or item.get("settleCurrency") or "").strip(),
            group_id=item.get("group_id") if item.get("group_id") is not None else group_id,
        )
        if not result.get("ok"):
            errors.append({"index": index, "market": market, "symbol": symbol, "error": result.get("error")})
        elif result.get("status") == "added":
            added += 1
        else:
            updated += 1
    return {
        "total": len(items or []),
        "added": added,
        "updated": updated,
        "failed": len(errors),
        "errors": errors,
    }


def _resolve_owned_group_id(user_id: int, group_id: Optional[int]) -> Optional[int]:
    """Return the group id when it belongs to the user, else None."""
    if group_id is None:
        return None
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return None
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT id FROM qd_watchlist_groups WHERE id = ? AND user_id = ?",
            (gid, user_id),
        )
        row = cur.fetchone()
        cur.close()
    return gid if row else None


def list_watchlist_groups(user_id: int) -> list:
    """Return the user's groups ordered by sort_order, then name."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT g.id, g.name, g.sort_order,
                   (SELECT COUNT(*) FROM qd_watchlist w WHERE w.group_id = g.id) AS item_count
            FROM qd_watchlist_groups g
            WHERE g.user_id = ?
            ORDER BY g.sort_order, g.name
            """,
            (user_id,),
        )
        rows = cur.fetchall() or []
        cur.close()
    return rows


def create_watchlist_group(user_id: int, name: str, sort_order: int = 0) -> tuple[bool, str, Optional[dict]]:
    """Create a group; names are unique per user."""
    clean = (name or "").strip()
    if not clean:
        return False, "Group name is required", None
    if len(clean) > 100:
        return False, "Group name must be at most 100 characters", None
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_watchlist_groups (user_id, name, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, NOW(), NOW())
            ON CONFLICT (user_id, name) DO NOTHING
            RETURNING id, name, sort_order
            """,
            (user_id, clean, int(sort_order or 0)),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT id, name, sort_order FROM qd_watchlist_groups WHERE user_id = ? AND name = ?",
                (user_id, clean),
            )
            row = cur.fetchone()
            db.commit()
            cur.close()
            return True, "exists", dict(row) if row else None
        db.commit()
        cur.close()
    return True, "created", dict(row)


def update_watchlist_group(
    user_id: int,
    group_id: int,
    *,
    name: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> tuple[bool, str]:
    """Rename and/or reorder one group owned by the user."""
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return False, "Invalid group id"
    sets, params = [], []
    if name is not None:
        clean = name.strip()
        if not clean:
            return False, "Group name cannot be empty"
        if len(clean) > 100:
            return False, "Group name must be at most 100 characters"
        sets.append("name = ?")
        params.append(clean)
    if sort_order is not None:
        sets.append("sort_order = ?")
        params.append(int(sort_order))
    if not sets:
        return False, "Nothing to update"
    sets.append("updated_at = NOW()")
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"UPDATE qd_watchlist_groups SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            (*params, gid, user_id),
        )
        changed = cur.rowcount or 0
        db.commit()
        cur.close()
    if not changed:
        return False, "Group not found"
    return True, "success"


def delete_watchlist_group(user_id: int, group_id: int) -> bool:
    """Delete a group; its items fall back to ungrouped (group_id NULL)."""
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return False
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "DELETE FROM qd_watchlist_groups WHERE id = ? AND user_id = ?",
            (gid, user_id),
        )
        changed = cur.rowcount or 0
        db.commit()
        cur.close()
    return bool(changed)


def move_watchlist_items(user_id: int, item_ids: list, group_id: Optional[int]) -> dict:
    """Move items to a group (or to ungrouped when group_id is NULL)."""
    ids = []
    for raw in item_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {"moved": 0, "error": "No valid item ids"}
    owned_group_id = _resolve_owned_group_id(user_id, group_id)
    if group_id is not None and owned_group_id is None:
        return {"moved": 0, "error": f"Group {group_id} not found"}
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_watchlist SET group_id = ?, updated_at = NOW()
            WHERE user_id = ? AND id = ANY(?)
            """,
            (owned_group_id, user_id, ids),
        )
        moved = cur.rowcount or 0
        db.commit()
        cur.close()
    return {"moved": moved}


def set_watchlist_order(user_id: int, ordered_item_ids: list) -> dict:
    """Persist manual ordering: index in the list becomes sort_order."""
    ids = []
    for raw in ordered_item_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return {"ordered": 0, "error": "Invalid item id in order list"}
    if not ids:
        return {"ordered": 0, "error": "Empty order list"}
    with get_db_connection() as db:
        cur = db.cursor()
        for position, item_id in enumerate(ids):
            cur.execute(
                """
                UPDATE qd_watchlist SET sort_order = ?, updated_at = NOW()
                WHERE user_id = ? AND id = ?
                """,
                (position, user_id, item_id),
            )
        db.commit()
        cur.close()
    return {"ordered": len(ids)}


def remove_watchlist_item(
    user_id: int,
    market: str,
    raw_symbol: str,
    *,
    exchange_id: str = "",
    market_type: str = "",
    instrument_id: str = "",
) -> bool:
    """Remove one market context, or all legacy contexts when none is supplied."""
    market = (market or "").strip()
    raw_symbol = normalize_symbol(raw_symbol)
    canonical_symbol = normalize_crypto_symbol(raw_symbol) if market == "Crypto" else raw_symbol

    with get_db_connection() as db:
        cur = db.cursor()
        has_market_context = bool(exchange_id or market_type or instrument_id)
        if market and has_market_context:
            context = MarketContext.from_mapping({
                "market": market,
                "symbol": canonical_symbol,
                "exchange_id": exchange_id,
                "market_type": market_type,
                "instrument_id": instrument_id,
            })
            clauses = ["user_id = ?", "market = ?", "symbol = ?"]
            params = [user_id, market, canonical_symbol]
            if exchange_id:
                clauses.append("exchange_id = ?")
                params.append(context.exchange_id)
            if market_type:
                clauses.append("market_type = ?")
                params.append(context.market_type)
            if instrument_id:
                clauses.append("instrument_id = ?")
                params.append(context.instrument_id)
            cur.execute(f"DELETE FROM qd_watchlist WHERE {' AND '.join(clauses)}", tuple(params))
        elif market:
            cur.execute(
                "DELETE FROM qd_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
                (user_id, market, canonical_symbol),
            )
            deleted = cur.rowcount or 0
            if deleted == 0 and canonical_symbol != raw_symbol:
                cur.execute(
                    "DELETE FROM qd_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
                    (user_id, market, raw_symbol),
                )
        else:
            logger.info(
                "remove_watchlist called without market (user=%s, symbol=%s); using legacy symbol-only delete",
                user_id,
                raw_symbol,
            )
            cur.execute(
                "DELETE FROM qd_watchlist WHERE user_id = ? AND symbol = ?",
                (user_id, raw_symbol),
            )
        db.commit()
        cur.close()
    return True


def get_user_watchlist_pairs(user_id: int) -> list:
    """Return exact market contexts for quote fetching."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT market, symbol, exchange_id, market_type,
                   instrument_id, settle_currency
            FROM qd_watchlist
            WHERE user_id = ?
            """,
            (user_id,),
        )
        rows = cur.fetchall() or []
        cur.close()
    out = []
    seen = set()
    for row in rows:
        context_key = (
            row.get("market"), row.get("symbol"), row.get("exchange_id"),
            row.get("market_type"), row.get("instrument_id"),
        )
        if context_key in seen:
            continue
        seen.add(context_key)
        out.append({
            "market": row.get("market") or "",
            "symbol": row.get("symbol") or "",
            "exchange_id": row.get("exchange_id") or "",
            "market_type": row.get("market_type") or "",
            "instrument_id": row.get("instrument_id") or "",
            "settle_currency": row.get("settle_currency") or "",
        })
    return out


def resolve_symbol_name_bounded(market: str, symbol: str, timeout_sec: float = 4.0) -> Optional[str]:
    """Resolve a display name with a hard wall-clock cap."""
    try:
        future = _name_resolve_executor.submit(resolve_symbol_name, market, symbol)
        return future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        logger.info(
            "Symbol name resolve timed out after %.1fs for %s:%s",
            timeout_sec,
            market,
            symbol,
        )
        return None
    except Exception as exc:
        logger.debug("Symbol name resolve raised for %s:%s: %s", market, symbol, exc)
        return None


def _backfill_row_name(cur, user_id: int, row: dict) -> None:
    try:
        market = row.get("market")
        symbol = row.get("symbol")
        current_name = (row.get("name") or "").strip()
        if not market or not symbol or (current_name and current_name != symbol):
            return
        resolved = resolve_symbol_name(market, symbol) or seed_get_symbol_name(market, symbol)
        if resolved and resolved != current_name:
            row["name"] = resolved
            cur.execute(
                "UPDATE qd_watchlist SET name = ?, updated_at = NOW() WHERE id = ? AND user_id = ?",
                (resolved, row.get("id"), user_id),
            )
    except Exception:
        return

