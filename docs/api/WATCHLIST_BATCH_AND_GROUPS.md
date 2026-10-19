# Watchlist batch operations and grouping

Backend-only feature (issues #1–#4). The watchlist supports user-defined
groups, batch add, move between groups, and manual ordering. Existing
single-item endpoints are unchanged; `watchlist/get` additionally returns
`group_id`, `group_name`, `sort_order`, and `group_sort_order` per row.

## Groups

```bash
TOKEN="<auth token>"
BASE="http://127.0.0.1:15000/api/market/watchlist"

# List groups (with item counts)
curl -H "Authorization: Bearer $TOKEN" $BASE/groups

# Create (re-creating an existing name returns it with msg "exists")
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "银行股", "sortOrder": 1}' $BASE/groups

# Rename / reorder
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "金融", "sortOrder": 2}' $BASE/groups/7

# Delete — items fall back to ungrouped, they are never deleted
curl -X DELETE -H "Authorization: Bearer $TOKEN" $BASE/groups/7
```

## Batch add

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
        "items": [
          {"market": "CNStock", "symbol": "600000", "name": "浦发银行"},
          {"market": "CNStock", "symbol": "000001"}
        ],
        "group_id": 7
      }' $BASE/addBatch
# -> {"code":1,"data":{"total":2,"added":2,"updated":0,"failed":0,"errors":[]}}
```

Per-item `group_id` overrides the request-level `group_id`. Re-adding an
existing symbol updates it (name refresh / group assignment) instead of
duplicating; duplicates inside one request are reported in `errors`.
At most 500 items per call.

## Move and reorder

```bash
# Move items to a group (null group_id -> ungrouped)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"itemIds": [11, 12], "group_id": 7}' $BASE/move

# Persist manual order (list position becomes sort_order)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"itemIds": [12, 11]}' $BASE/reorder
```

## Bulk import from file

`scripts/watchlist_import.py` reads a CSV (header `market,symbol,name,group`;
`name`/`group` optional) or plain text (`market,symbol[,name[,group]]` per
line), creates missing groups, and pushes items through `addBatch` in
chunks of 200:

```bash
cd backend_api_python
python scripts/watchlist_import.py --file my_list.csv \
  --base-url http://127.0.0.1:15000 \
  --username quantdinger --password '<password>'
```

## Semantics

- All operations are scoped to the authenticated user; group ids from
  other users are rejected as not found.
- Deleting a group keeps its items (FK `ON DELETE SET NULL`).
- `sort_order` defaults to 0; listing orders by group sort order first,
  then item `sort_order`, then newest first.
