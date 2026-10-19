"""Watchlist batch operations and grouping (issues #1-#3).

Covers the service layer contract: group lifecycle, batch dedupe,
move semantics, delete-group-keeps-items, ordering, and tenant isolation.
Route-level auth is exercised through the Flask test client.
"""
import pytest

from app.services.market.watchlist import (
    add_watchlist_items,
    create_watchlist_group,
    delete_watchlist_group,
    list_watchlist,
    list_watchlist_groups,
    move_watchlist_items,
    set_watchlist_order,
    update_watchlist_group,
)
from app.utils.auth import generate_token
from app.utils.db import get_db_connection


@pytest.fixture()
def user_a_id():
    with get_db_connection() as db:
        return _ensure_user(db, "wl_test_a")


@pytest.fixture()
def user_b_id():
    with get_db_connection() as db:
        return _ensure_user(db, "wl_test_b")


@pytest.fixture()
def auth_headers(user_a_id):
    token = generate_token(user_a_id, "wl_test_a", role="viewer")
    return {"Authorization": f"Bearer {token}"}


def _ensure_user(db, username: str) -> int:
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO qd_users (username, password_hash, email, nickname, role, status)
        VALUES (?, 'x', ?, ?, 'viewer', 'active')
        ON CONFLICT DO NOTHING
        """,
        (username, f"{username}@example.com", username),
    )
    cur.execute("SELECT id FROM qd_users WHERE username = ?", (username,))
    row = cur.fetchone()
    db.commit()
    cur.close()
    return int(row["id"])


def _cleanup(db, user_id: int):
    cur = db.cursor()
    cur.execute("DELETE FROM qd_watchlist WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM qd_watchlist_groups WHERE user_id = ?", (user_id,))
    db.commit()
    cur.close()


@pytest.fixture(autouse=True)
def _isolate_users(user_a_id, user_b_id):
    _cleanup_user(user_a_id)
    _cleanup_user(user_b_id)
    yield
    _cleanup_user(user_a_id)
    _cleanup_user(user_b_id)


def _cleanup_user(user_id: int):
    with get_db_connection() as db:
        _cleanup(db, user_id)


def _group(name: str) -> dict:
    return {"market": "Crypto", "symbol": "BTC/USDT", "name": name}


class TestGroupLifecycle:
    def test_create_list_rename_delete(self, user_a_id):
        ok, msg, group = create_watchlist_group(user_a_id, "Core")
        assert ok and msg == "created" and group["name"] == "Core"

        ok, msg, existing = create_watchlist_group(user_a_id, "Core")
        assert ok and msg == "exists" and existing["id"] == group["id"]

        groups = list_watchlist_groups(user_a_id)
        assert [g["name"] for g in groups] == ["Core"]
        assert groups[0]["item_count"] == 0

        ok, msg = update_watchlist_group(user_a_id, group["id"], name="Core Holdings", sort_order=5)
        assert ok
        groups = list_watchlist_groups(user_a_id)
        assert groups[0]["name"] == "Core Holdings"
        assert groups[0]["sort_order"] == 5

        assert delete_watchlist_group(user_a_id, group["id"]) is True
        assert list_watchlist_groups(user_a_id) == []

    def test_rename_to_missing_group_fails(self, user_a_id):
        ok, msg = update_watchlist_group(user_a_id, 999999, name="nope")
        assert not ok and msg == "Group not found"

    def test_empty_and_long_names_rejected(self, user_a_id):
        assert create_watchlist_group(user_a_id, "  ")[0] is False
        assert create_watchlist_group(user_a_id, "x" * 101)[0] is False

    def test_groups_are_per_user(self, user_a_id, user_b_id):
        create_watchlist_group(user_a_id, "OnlyA")
        assert list_watchlist_groups(user_b_id) == []
        # B cannot rename or delete A's group
        gid = list_watchlist_groups(user_a_id)[0]["id"]
        assert update_watchlist_group(user_b_id, gid, name="Hacked")[0] is False
        assert delete_watchlist_group(user_b_id, gid) is False


class TestBatchAdd:
    def test_batch_add_and_idempotent_rerun(self, user_a_id):
        result = add_watchlist_items(user_a_id, [
            {"market": "Crypto", "symbol": "BTC/USDT", "name": "Bitcoin"},
            {"market": "Crypto", "symbol": "ETH/USDT"},
        ])
        assert result["added"] == 2 and result["failed"] == 0
        assert len(list_watchlist(user_a_id)) == 2

        rerun = add_watchlist_items(user_a_id, [
            {"market": "Crypto", "symbol": "BTC/USDT"},
            {"market": "Crypto", "symbol": "SOL/USDT"},
        ])
        assert rerun["added"] == 1 and rerun["updated"] == 1
        assert len(list_watchlist(user_a_id)) == 3

    def test_duplicates_in_one_request_reported(self, user_a_id):
        result = add_watchlist_items(user_a_id, [
            {"market": "Crypto", "symbol": "BTC/USDT"},
            {"market": "Crypto", "symbol": "BTC/USDT"},
        ])
        assert result["added"] == 1
        assert result["failed"] == 1
        assert "Duplicate" in result["errors"][0]["error"]

    def test_invalid_items_fail_without_blocking_valid_ones(self, user_a_id):
        result = add_watchlist_items(user_a_id, [
            {"market": "Bogus", "symbol": "XXX"},
            {"market": "Crypto", "symbol": "not-a-pair"},
            {"market": "Crypto", "symbol": "BTC/USDT"},
        ])
        assert result["added"] == 1 and result["failed"] == 2

    def test_batch_into_group(self, user_a_id):
        _, _, group = create_watchlist_group(user_a_id, "Core")
        result = add_watchlist_items(user_a_id, [_group("a")], group_id=group["id"])
        assert result["added"] == 1
        row = list_watchlist(user_a_id)[0]
        assert row["group_id"] == group["id"]
        assert row["group_name"] == "Core"

    def test_unknown_group_rejects(self, user_a_id):
        result = add_watchlist_items(user_a_id, [_group("a")], group_id=424242)
        assert result["failed"] == 1
        assert "not found" in result["errors"][0]["error"]


class TestMoveAndDelete:
    def test_move_items_between_groups_and_ungrouped(self, user_a_id):
        add_watchlist_items(user_a_id, [_group("a"), {"market": "Crypto", "symbol": "ETH/USDT"}])
        _, _, g1 = create_watchlist_group(user_a_id, "One")
        _, _, g2 = create_watchlist_group(user_a_id, "Two")
        rows = {r["symbol"]: r for r in list_watchlist(user_a_id)}
        btc_id, eth_id = rows["BTC/USDT"]["id"], rows["ETH/USDT"]["id"]

        assert move_watchlist_items(user_a_id, [btc_id, eth_id], g1["id"])["moved"] == 2
        rows = {r["symbol"]: r for r in list_watchlist(user_a_id)}
        assert rows["BTC/USDT"]["group_name"] == "One"

        assert move_watchlist_items(user_a_id, [eth_id], g2["id"])["moved"] == 1
        assert move_watchlist_items(user_a_id, [eth_id], None)["moved"] == 1
        rows = {r["symbol"]: r for r in list_watchlist(user_a_id)}
        assert rows["ETH/USDT"]["group_id"] is None

    def test_move_to_foreign_group_rejected(self, user_a_id, user_b_id):
        add_watchlist_items(user_a_id, [_group("a")])
        _, _, b_group = create_watchlist_group(user_b_id, "BelongsToB")
        row = list_watchlist(user_a_id)[0]
        result = move_watchlist_items(user_a_id, [row["id"]], b_group["id"])
        assert result["moved"] == 0 and "not found" in result["error"]

    def test_delete_group_keeps_items_ungrouped(self, user_a_id):
        _, _, group = create_watchlist_group(user_a_id, "Temp")
        add_watchlist_items(user_a_id, [_group("a")], group_id=group["id"])
        assert delete_watchlist_group(user_a_id, group["id"]) is True
        rows = list_watchlist(user_a_id)
        assert len(rows) == 1
        assert rows[0]["group_id"] is None


class TestOrdering:
    def test_order_is_persisted(self, user_a_id):
        add_watchlist_items(user_a_id, [
            {"market": "Crypto", "symbol": "BTC/USDT"},
            {"market": "Crypto", "symbol": "ETH/USDT"},
            {"market": "Crypto", "symbol": "SOL/USDT"},
        ])
        by_symbol = {r["symbol"]: r for r in list_watchlist(user_a_id)}
        result = set_watchlist_order(user_a_id, [
            by_symbol["SOL/USDT"]["id"],
            by_symbol["BTC/USDT"]["id"],
            by_symbol["ETH/USDT"]["id"],
        ])
        assert result["ordered"] == 3
        ordered = [r["symbol"] for r in list_watchlist(user_a_id)]
        assert ordered == ["SOL/USDT", "BTC/USDT", "ETH/USDT"]

    def test_invalid_order_input(self, user_a_id):
        assert set_watchlist_order(user_a_id, ["abc"])["ordered"] == 0
        assert set_watchlist_order(user_a_id, [])["ordered"] == 0


class TestRoutes:
    """Auth + envelope checks through the Flask test client."""

    def test_endpoints_require_login(self, client):
        for method, path in [
            ("post", "/api/market/watchlist/addBatch"),
            ("get", "/api/market/watchlist/groups"),
            ("post", "/api/market/watchlist/groups"),
            ("put", "/api/market/watchlist/groups/1"),
            ("delete", "/api/market/watchlist/groups/1"),
            ("post", "/api/market/watchlist/move"),
            ("post", "/api/market/watchlist/reorder"),
        ]:
            resp = getattr(client, method)(path, json={})
            assert resp.status_code == 401, f"{method} {path} should require login"

    def test_add_batch_validation(self, client, auth_headers):
        resp = client.post("/api/market/watchlist/addBatch", headers=auth_headers, json={"items": "nope"})
        assert resp.status_code == 400
        resp = client.post("/api/market/watchlist/addBatch", headers=auth_headers, json={})
        assert resp.status_code == 400

    def test_group_crud_via_routes(self, client, auth_headers):
        resp = client.post("/api/market/watchlist/groups", headers=auth_headers, json={"name": "ViaAPI"})
        assert resp.status_code == 200 and resp.json["code"] == 1
        gid = resp.json["data"]["id"]

        resp = client.put(f"/api/market/watchlist/groups/{gid}", headers=auth_headers, json={"name": "Renamed"})
        assert resp.status_code == 200

        resp = client.get("/api/market/watchlist/groups", headers=auth_headers)
        assert resp.json["data"][0]["name"] == "Renamed"

        resp = client.delete(f"/api/market/watchlist/groups/{gid}", headers=auth_headers)
        assert resp.status_code == 200
        resp = client.delete(f"/api/market/watchlist/groups/{gid}", headers=auth_headers)
        assert resp.status_code == 404
