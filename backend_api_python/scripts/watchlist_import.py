"""Bulk-import watchlist symbols from a CSV or plain-text file.

Usage (from backend_api_python/, inside or outside Docker):

    python -m app.scripts... (not registered) — run directly:

    python scripts/watchlist_import.py --file my_list.csv \
        --base-url http://127.0.0.1:15000 --username quantdinger --password <pw>

CSV columns (header required):
    market,symbol,name,group
`group` is optional; groups are created on demand. Blank lines and `#`
comments are ignored. `name` is optional too.

    market,symbol,name,group
    CNStock,600000,浦发银行,银行
    CNStock,000001,平安银行,银行
    USStock,AAPL,Apple,科技

Plain-text fallback (no header): one `market,symbol[,name[,group]]` per line.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request

BASE_URL_DEFAULT = "http://127.0.0.1:15000"
VALID_MARKETS = {"Crypto", "USStock", "CNStock", "HKStock", "Forex", "Futures", "MOEX"}


def parse_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        has_header = sample.lstrip().lower().startswith("market")
        if has_header:
            for row in csv.DictReader(fh):
                rows.append({
                    "market": (row.get("market") or "").strip(),
                    "symbol": (row.get("symbol") or "").strip(),
                    "name": (row.get("name") or "").strip(),
                    "group": (row.get("group") or "").strip(),
                })
        else:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                rows.append({
                    "market": parts[0],
                    "symbol": parts[1],
                    "name": parts[2] if len(parts) > 2 else "",
                    "group": parts[3] if len(parts) > 3 else "",
                })
    return [r for r in rows if r["market"] and r["symbol"]]


def api(base_url: str, token: str, method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def login(base_url: str, username: str, password: str) -> str:
    body = api(base_url, "", "POST", "/api/auth/login", {"username": username, "password": password})
    data = body.get("data") or {}
    token = data.get("token") or data.get("access_token") or ""
    if body.get("code") != 1 or not token:
        raise SystemExit(f"Login failed: {body.get('msg')}")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", required=True, help="CSV or text file to import")
    parser.add_argument("--base-url", default=BASE_URL_DEFAULT)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    rows = parse_rows(args.file)
    if not rows:
        print("No valid rows found in", args.file)
        return 1

    bad = [r for r in rows if r["market"] not in VALID_MARKETS]
    if bad:
        for r in bad:
            print(f"SKIP invalid market: {r['market']}:{r['symbol']}", file=sys.stderr)
        rows = [r for r in rows if r["market"] in VALID_MARKETS]
    if not rows:
        return 1

    token = login(args.base_url, args.username, args.password)

    group_ids: dict[str, int] = {}
    existing = {g["name"]: g["id"] for g in api(args.base_url, token, "GET", "/api/market/watchlist/groups").get("data") or []}
    for row in rows:
        gname = row["group"]
        if gname and gname not in existing:
            body = api(args.base_url, token, "POST", "/api/market/watchlist/groups", {"name": gname})
            gid = (body.get("data") or {}).get("id")
            if gid:
                existing[gname] = gid
    group_ids = existing

    items = [
        {
            "market": r["market"],
            "symbol": r["symbol"],
            "name": r["name"],
            "group_id": group_ids.get(r["group"]) if r["group"] else None,
        }
        for r in rows
    ]
    summary = {"added": 0, "updated": 0, "failed": 0}
    for chunk_start in range(0, len(items), 200):
        chunk = items[chunk_start:chunk_start + 200]
        body = api(args.base_url, token, "POST", "/api/market/watchlist/addBatch", {"items": chunk})
        data = body.get("data") or {}
        for key in summary:
            summary[key] += data.get(key, 0)
        for err in data.get("errors") or []:
            print(f"ERROR item {err.get('index')}: {err.get('error')}", file=sys.stderr)

    print(
        f"Import finished: {summary['added']} added, {summary['updated']} updated, "
        f"{summary['failed']} failed (groups: {', '.join(sorted(existing)) or '-'})"
    )
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
