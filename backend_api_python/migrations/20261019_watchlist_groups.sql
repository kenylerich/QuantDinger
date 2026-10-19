-- Watchlist groups and item ordering (issue #1).
-- Idempotent: safe to re-apply on every boot.

CREATE TABLE IF NOT EXISTS qd_watchlist_groups (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_watchlist_group_name UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_groups_user_id
  ON qd_watchlist_groups(user_id);

ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS group_id INTEGER
  REFERENCES qd_watchlist_groups(id) ON DELETE SET NULL;
ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_watchlist_group_id
  ON qd_watchlist(group_id);

-- Deleting a group keeps its items: the FK above falls them back to
-- "ungrouped" (group_id NULL) instead of cascading a delete.
