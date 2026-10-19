from app.services import user_service
from app.services.market import symbol_search, watchlist


class _CaptureCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def close(self):
        pass


class _CaptureConn:
    def __init__(self):
        self.cursor_obj = _CaptureCursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


def test_find_market_symbol_requires_exact_external_match(monkeypatch):
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda **kwargs: [])
    monkeypatch.setattr(
        symbol_search,
        "_search_external_symbols",
        lambda market, keyword, limit, existing: [
            {"market": "USStock", "symbol": "AAP", "name": "Advance Auto Parts"}
        ],
    )

    assert symbol_search.find_market_symbol("USStock", "AAPL") is None


def test_find_market_symbol_accepts_exact_external_match(monkeypatch):
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda **kwargs: [])
    monkeypatch.setattr(
        symbol_search,
        "_search_external_symbols",
        lambda market, keyword, limit, existing: [
            {"market": "USStock", "symbol": "AAPL", "name": "Apple Inc."}
        ],
    )

    assert symbol_search.find_market_symbol("USStock", "AAPL") == {
        "market": "USStock",
        "symbol": "AAPL",
        "name": "Apple Inc.",
    }


def test_add_watchlist_rejects_crypto_symbol_not_in_registry(monkeypatch):
    monkeypatch.setattr(watchlist, "find_market_symbol", lambda *args, **kwargs: None)
    monkeypatch.setattr(watchlist, "get_db_connection", lambda: (_ for _ in ()).throw(AssertionError("DB write should not happen")))

    ok, message = watchlist.add_watchlist_item(1, "Crypto", "AAPL")

    assert ok is False
    assert "AAPL/USDT" in message
    assert "not found on Crypto" in message


def test_add_watchlist_persists_only_after_exact_symbol_match(monkeypatch):
    conn = _CaptureConn()
    monkeypatch.setattr(
        watchlist,
        "find_market_symbol",
        lambda market, symbol, **kwargs: {"market": market, "symbol": symbol, "name": "Apple Inc."},
    )
    monkeypatch.setattr(watchlist, "persist_seed_name", lambda market, symbol, name: None)
    monkeypatch.setattr(watchlist, "get_db_connection", lambda: conn)

    ok, message = watchlist.add_watchlist_item(1, "USStock", "AAPL")

    assert ok is True
    assert message == "success"
    assert conn.committed is True
    assert conn.cursor_obj.executed
    _, params = conn.cursor_obj.executed[0]
    assert params == (1, "USStock", "AAPL", "Apple Inc.", "", "spot", "", "", None)  # + group_id


def test_crypto_hot_symbols_include_default_source_identity(monkeypatch):
    monkeypatch.setattr(
        symbol_search,
        "seed_get_hot_symbols",
        lambda market, limit: [
            {"market": "Crypto", "symbol": "BTC/USDT", "name": "Bitcoin"},
            {"market": "Crypto", "symbol": "MISSING/USDT", "name": "Missing"},
        ],
    )
    monkeypatch.setattr(symbol_search, "default_crypto_exchange_id", lambda: "okx")
    monkeypatch.setattr(
        symbol_search,
        "find_available_crypto_symbol",
        lambda symbol, **kwargs: (
            {
                "market": "Crypto",
                "symbol": symbol,
                "name": symbol,
                "exchange_id": "okx",
                "market_type": "swap",
                "instrument_id": "BTC-USDT-SWAP",
                "settle_currency": "USDT",
            }
            if symbol == "BTC/USDT"
            else None
        ),
    )

    rows = symbol_search.get_hot_symbols("Crypto", 10)

    assert rows == [{
        "market": "Crypto",
        "symbol": "BTC/USDT",
        "name": "Bitcoin",
        "exchange_id": "okx",
        "market_type": "swap",
        "instrument_id": "BTC-USDT-SWAP",
        "settle_currency": "USDT",
        "asset_class": "crypto",
        "product_type": "crypto",
        "api_family": "swap",
        "underlying_market": "",
        "underlying_symbol": "",
        "product_meta": {},
    }]


def test_crypto_add_persists_exact_exchange_binding(monkeypatch):
    conn = _CaptureConn()
    monkeypatch.setattr(
        watchlist,
        "find_market_symbol",
        lambda market, symbol, **kwargs: {
            "market": "Crypto",
            "symbol": symbol,
            "name": "Bitcoin",
            "exchange_id": "okx",
            "market_type": "swap",
            "instrument_id": "BTC-USDT-SWAP",
            "settle_currency": "USDT",
        },
    )
    monkeypatch.setattr(watchlist, "persist_seed_name", lambda market, symbol, name: None)
    monkeypatch.setattr(watchlist, "get_db_connection", lambda: conn)

    ok, message = watchlist.add_watchlist_item(1, "Crypto", "BTC/USDT")

    assert ok is True
    assert message == "success"
    assert len(conn.cursor_obj.executed) == 1
    insert_sql, insert_params = conn.cursor_obj.executed[0]
    assert "INSERT INTO qd_watchlist" in insert_sql
    assert "ON CONFLICT(user_id, market, symbol, exchange_id, market_type, instrument_id) DO UPDATE SET" in " ".join(insert_sql.split())
    assert insert_params == (
        1,
        "Crypto",
        "BTC/USDT",
        "Bitcoin",
        "okx",
        "swap",
        "BTC-USDT-SWAP",
        "USDT",
        None,  # group_id
    )


def test_gate_hk_stock_watchlist_keeps_native_exchange_contract(monkeypatch):
    conn = _CaptureConn()
    monkeypatch.setattr(
        watchlist,
        "find_market_symbol",
        lambda market, symbol, **kwargs: {
            "market": "Crypto",
            "symbol": "00700/HKD",
            "name": "Tencent",
            "exchange_id": "gate",
            "market_type": "spot",
            "instrument_id": "00700",
            "settle_currency": "HKD",
            "product_type": "direct_equity",
            "underlying_market": "HKStock",
            "underlying_symbol": "00700",
        },
    )
    monkeypatch.setattr(watchlist, "persist_seed_name", lambda market, symbol, name: None)
    monkeypatch.setattr(watchlist, "get_db_connection", lambda: conn)

    ok, message = watchlist.add_watchlist_item(
        1,
        "Crypto",
        "00700/HKD",
        exchange_id="gate",
        market_type="spot",
        instrument_id="00700",
    )

    assert ok is True
    assert message == "success"
    _, params = conn.cursor_obj.executed[0]
    assert params == (
        1,
        "Crypto",
        "00700/HKD",
        "Tencent",
        "gate",
        "spot",
        "00700",
        "HKD",
        None,  # group_id
    )


def test_default_watchlist_seed_uses_market_context_unique_key(monkeypatch):
    conn = _CaptureConn()
    monkeypatch.setattr(
        user_service,
        "_DEFAULT_WATCHLIST",
        [("Crypto", "BTC/USDT", "Bitcoin")],
    )

    user_service._seed_default_watchlist(conn, 9)

    assert conn.committed is True
    assert len(conn.cursor_obj.executed) == 1
    insert_sql, insert_params = conn.cursor_obj.executed[0]
    assert "ON CONFLICT (user_id, market, symbol, exchange_id, market_type, instrument_id) DO NOTHING" in " ".join(insert_sql.split())
    assert insert_params == (9, "Crypto", "BTC/USDT", "Bitcoin", "", "spot")
