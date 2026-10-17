-- Picky database schema (contracts v0.1.0). Postgres 17 + pgvector (Docker image pgvector/pgvector:pg17).
-- Mirrors the pydantic models in contracts/*. Money columns are INTEGER cents. Geometry is meters / radians in the
-- store world frame. Seeded from data/ by `make seed` (drop + recreate; no migrations during the hackathon).
-- Nested model fields that are not queried are stored as JSONB with the model name in a comment.

CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------------------------------------------ catalog
CREATE TABLE IF NOT EXISTS stores (
    store_id        TEXT PRIMARY KEY,                       -- 'store_ave', 'store_hill', 'online'
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'physical' CHECK (kind IN ('physical', 'online')),
    address         TEXT,
    width_m         DOUBLE PRECISION,                       -- extent along +x
    depth_m         DOUBLE PRECISION,                       -- extent along +y
    planogram       JSONB,                                  -- contracts.planogram.Planogram (NULL for online)
    shipping_cents  INTEGER CHECK (shipping_cents >= 0),    -- online only
    free_shipping_over_cents  INTEGER CHECK (free_shipping_over_cents >= 0),  -- online only
    default_eta_hours         DOUBLE PRECISION,                               -- online only
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    sku              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    brand            TEXT,
    category         TEXT NOT NULL,
    subcategory      TEXT,
    description      TEXT NOT NULL DEFAULT '',
    tags             TEXT[] NOT NULL DEFAULT '{}',
    dietary          TEXT[] NOT NULL DEFAULT '{}',
    allergens        TEXT[] NOT NULL DEFAULT '{}',
    spice_level      SMALLINT CHECK (spice_level BETWEEN 0 AND 5),
    price_cents      INTEGER NOT NULL CHECK (price_cents >= 0),
    unit             TEXT NOT NULL DEFAULT 'each',
    size             TEXT,
    barcode          TEXT NOT NULL UNIQUE,
    perishable       BOOLEAN NOT NULL DEFAULT FALSE,
    shelf_life_days  INTEGER CHECK (shelf_life_days >= 0),
    online_only      BOOLEAN NOT NULL DEFAULT FALSE,
    image_url        TEXT,
    asset            JSONB NOT NULL DEFAULT '{}'::jsonb      -- contracts.catalog.SimAsset
);
CREATE INDEX IF NOT EXISTS products_category_idx ON products (category);
CREATE INDEX IF NOT EXISTS products_tags_idx ON products USING GIN (tags);

CREATE TABLE IF NOT EXISTS product_embeddings (
    sku         TEXT PRIMARY KEY REFERENCES products (sku) ON DELETE CASCADE,
    model       TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
    embedding   vector(384) NOT NULL,                        -- contracts.EMBED_DIM
    text        TEXT NOT NULL,                               -- the text that was embedded
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_embeddings_hnsw ON product_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS slots (
    store_id          TEXT NOT NULL REFERENCES stores (store_id) ON DELETE CASCADE,
    slot_id           TEXT NOT NULL,
    sku               TEXT NOT NULL REFERENCES products (sku),
    fixture_id        TEXT NOT NULL,
    aisle_id          TEXT,
    aisle_label       TEXT,
    level             INTEGER NOT NULL DEFAULT 0 CHECK (level >= 0),
    idx               INTEGER NOT NULL DEFAULT 0 CHECK (idx >= 0),   -- Slot.index
    item_x            DOUBLE PRECISION NOT NULL,
    item_y            DOUBLE PRECISION NOT NULL,
    item_z            DOUBLE PRECISION NOT NULL DEFAULT 0,
    approach_x        DOUBLE PRECISION NOT NULL,
    approach_y        DOUBLE PRECISION NOT NULL,
    approach_theta    DOUBLE PRECISION NOT NULL DEFAULT 0,
    facings           INTEGER NOT NULL DEFAULT 1,
    capacity          INTEGER NOT NULL DEFAULT 12,
    price_cents       INTEGER CHECK (price_cents >= 0),        -- store override; NULL = products.price_cents
    shelf_tag_cents   INTEGER CHECK (shelf_tag_cents >= 0),    -- physical tag if different (mispricing)
    PRIMARY KEY (store_id, slot_id)
);
CREATE INDEX IF NOT EXISTS slots_sku_idx ON slots (sku);

-- Live stock per slot (updated by picks and scans). qty = sum(lots.qty) when lots exist.
CREATE TABLE IF NOT EXISTS inventory (
    store_id       TEXT NOT NULL,
    slot_id        TEXT NOT NULL,
    sku            TEXT NOT NULL REFERENCES products (sku),
    qty            INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    last_scan_at   TIMESTAMPTZ,
    last_scan_id   TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, slot_id),
    FOREIGN KEY (store_id, slot_id) REFERENCES slots (store_id, slot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lots (
    store_id    TEXT NOT NULL,
    slot_id     TEXT NOT NULL,
    lot_id      TEXT NOT NULL,
    sku         TEXT NOT NULL REFERENCES products (sku),
    qty         INTEGER NOT NULL CHECK (qty >= 0),
    expiry      DATE,
    freshness   REAL CHECK (freshness BETWEEN 0 AND 1),
    PRIMARY KEY (store_id, slot_id, lot_id),
    FOREIGN KEY (store_id, slot_id) REFERENCES slots (store_id, slot_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS lots_expiry_idx ON lots (store_id, expiry);

-- Online store stock (data/online_store.yaml)
CREATE TABLE IF NOT EXISTS online_items (
    sku          TEXT PRIMARY KEY REFERENCES products (sku),
    price_cents  INTEGER CHECK (price_cents >= 0),
    qty          INTEGER NOT NULL DEFAULT 100 CHECK (qty >= 0),
    eta_hours    DOUBLE PRECISION
);

-- Slot deals (Slot.deal is DealTerms without an id): the seed derives deal_id = '<store_id>:<slot_id>'.
CREATE TABLE IF NOT EXISTS deals (
    deal_id         TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL REFERENCES stores (store_id) ON DELETE CASCADE,
    sku             TEXT NOT NULL REFERENCES products (sku),
    kind            TEXT NOT NULL CHECK (kind IN ('markdown', 'percent_off', 'bogo', 'near_expiry')),
    price_cents     INTEGER NOT NULL CHECK (price_cents >= 0),
    original_cents  INTEGER CHECK (original_cents >= 0),
    percent_off     SMALLINT CHECK (percent_off BETWEEN 0 AND 100),
    reason          TEXT NOT NULL DEFAULT '',
    lot_id          TEXT,
    valid_until     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS deals_store_idx ON deals (store_id, sku);

CREATE TABLE IF NOT EXISTS scans (
    scan_id          TEXT PRIMARY KEY,
    store_id         TEXT NOT NULL REFERENCES stores (store_id),
    slot_id          TEXT,
    trip_id          TEXT,
    expected_sku     TEXT,
    observed_count   INTEGER NOT NULL DEFAULT 0,
    count_is_exact   BOOLEAN NOT NULL DEFAULT FALSE,
    mismatch         BOOLEAN NOT NULL DEFAULT FALSE,
    shelf_tag_cents  INTEGER CHECK (shelf_tag_cents >= 0),
    detections       JSONB NOT NULL DEFAULT '[]'::jsonb,     -- list[contracts.perception.Detection]
    freshness        JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {instance_id: score}
    lot_freshness    JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {lot_id: score}
    pose_x           DOUBLE PRECISION,                       -- ScanReport.pose
    pose_y           DOUBLE PRECISION,
    pose_theta       DOUBLE PRECISION,
    frame_id         TEXT,
    latency_ms       REAL NOT NULL DEFAULT 0,
    notes            TEXT NOT NULL DEFAULT '',
    ts               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scans_store_ts_idx ON scans (store_id, ts DESC);

-- ------------------------------------------------------------------------------------------------ users
CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    phone          TEXT UNIQUE,                               -- Photon / iMessage handle (fictional demo users)
    email          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id               TEXT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    home_store_id         TEXT REFERENCES stores (store_id),
    household_size        INTEGER NOT NULL DEFAULT 1,
    prefs                 JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {tag: weight in [-1, 1]}
    dislikes              TEXT[] NOT NULL DEFAULT '{}',
    disliked_skus         TEXT[] NOT NULL DEFAULT '{}',
    favorite_skus         TEXT[] NOT NULL DEFAULT '{}',
    dietary               TEXT[] NOT NULL DEFAULT '{}',
    allergies             TEXT[] NOT NULL DEFAULT '{}',
    default_budget_cents  INTEGER CHECK (default_budget_cents >= 0),
    taste_vector          vector(384),
    version               INTEGER NOT NULL DEFAULT 1,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profile_updates (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    trip_id         TEXT,
    source          TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    changes         JSONB NOT NULL DEFAULT '[]'::jsonb,       -- list[contracts.profile.ProfileChange]
    version_before  INTEGER NOT NULL,
    version_after   INTEGER NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mandates (
    mandate_id                 TEXT PRIMARY KEY,
    user_id                    TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    cap_cents                  INTEGER NOT NULL CHECK (cap_cents >= 0),
    per_item_cap_cents         INTEGER CHECK (per_item_cap_cents >= 0),
    ask_above_cents            INTEGER NOT NULL CHECK (ask_above_cents >= 0),
    allowed_categories         TEXT[] NOT NULL DEFAULT '{}',
    blocked_categories         TEXT[] NOT NULL DEFAULT '{}',
    allowed_merchants          TEXT[] NOT NULL DEFAULT '{}',
    allow_online               BOOLEAN NOT NULL DEFAULT TRUE,
    require_checkout_approval  BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at                 TIMESTAMPTZ NOT NULL,
    revoked                    BOOLEAN NOT NULL DEFAULT FALSE,
    signature                  TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mandates_user_idx ON mandates (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id       TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    trip_id           TEXT,
    sku               TEXT NOT NULL REFERENCES products (sku),
    qty               INTEGER NOT NULL CHECK (qty >= 1),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    store_id          TEXT NOT NULL,
    rating            SMALLINT CHECK (rating BETWEEN 1 AND 5),
    ts                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS purchases_user_idx ON purchases (user_id, ts DESC);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    trip_id      TEXT,
    sku          TEXT REFERENCES products (sku),
    rating       SMALLINT CHECK (rating BETWEEN 1 AND 5),
    text         TEXT NOT NULL DEFAULT '',
    channel      TEXT NOT NULL CHECK (channel IN ('web', 'voice', 'photon')),
    tags         TEXT[] NOT NULL DEFAULT '{}',
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------------------------------------ trips
CREATE TABLE IF NOT EXISTS trips (
    trip_id        TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users (user_id),
    user_ids       TEXT[] NOT NULL DEFAULT '{}',
    store_id       TEXT NOT NULL REFERENCES stores (store_id),
    state          TEXT NOT NULL CHECK (state IN ('intent', 'list_ready', 'recs_ready', 'route_planned',
                     'awaiting_login', 'executing', 'awaiting_approval', 'checkout', 'paid', 'followup', 'done',
                     'failed')),
    intent         JSONB,                                    -- contracts.shopping.Intent
    shopping_list  JSONB,                                    -- contracts.shopping.ShoppingList
    route          JSONB,                                    -- contracts.routing.Route
    snapshot       JSONB,                                    -- full contracts.trip.Trip (source of truth for resume,
                                                             -- incl. stop_status and followup)
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trips_user_idx ON trips (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cart_lines (
    trip_id           TEXT NOT NULL REFERENCES trips (trip_id) ON DELETE CASCADE,
    line_id           TEXT NOT NULL,
    sku               TEXT NOT NULL REFERENCES products (sku),
    name              TEXT,
    category          TEXT NOT NULL DEFAULT 'uncategorized',
    qty               INTEGER NOT NULL CHECK (qty >= 1),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    source            TEXT NOT NULL CHECK (source IN ('in_store', 'online')),
    store_id          TEXT NOT NULL,
    slot_id           TEXT,                                  -- in-store slot picked from (heatmap picks)
    status            TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'picked', 'verified', 'declined')),
    rec_id            TEXT,
    deal_id           TEXT,
    list_item_id      TEXT,
    user_id           TEXT,
    instance_ids      TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (trip_id, line_id)
);

CREATE TABLE IF NOT EXISTS recommendations (
    rec_id            TEXT PRIMARY KEY,
    trip_id           TEXT REFERENCES trips (trip_id) ON DELETE CASCADE,
    user_id           TEXT NOT NULL REFERENCES users (user_id),
    sku               TEXT NOT NULL REFERENCES products (sku),
    product_name      TEXT,
    store_id          TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('detour', 'checkout_batch', 'near_expiry_offer')),
    interest          REAL NOT NULL CHECK (interest BETWEEN 0 AND 1),
    reason            TEXT NOT NULL,
    signals           JSONB NOT NULL DEFAULT '{}'::jsonb,
    sponsored         BOOLEAN NOT NULL DEFAULT FALSE,        -- must be labeled in UI when true
    slot_id           TEXT,
    price_cents       INTEGER CHECK (price_cents >= 0),     -- unit price
    deal              JSONB,                                -- contracts.catalog.Deal
    extra_distance_m  DOUBLE PRECISION,                     -- from routing Stop.detour_m
    extra_time_s      DOUBLE PRECISION,                     -- from routing Stop.detour_s
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Recommendation transparency log (trust.TransparencyEntry): why each item was suggested, and what happened.
-- record_transparency UPSERTs on rec_id (entry_id = rec_id by convention).
CREATE TABLE IF NOT EXISTS transparency (
    entry_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users (user_id),
    trip_id    TEXT,
    rec_id     TEXT NOT NULL UNIQUE,
    sku        TEXT NOT NULL,
    store_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    signals    JSONB NOT NULL DEFAULT '{}'::jsonb,
    interest   REAL NOT NULL DEFAULT 0,
    sponsored  BOOLEAN NOT NULL DEFAULT FALSE,
    outcome    TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'accepted', 'declined', 'ignored')),
    shown_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id    TEXT PRIMARY KEY,
    trip_id        TEXT NOT NULL REFERENCES trips (trip_id) ON DELETE CASCADE,
    user_id        TEXT NOT NULL REFERENCES users (user_id),
    kind           TEXT NOT NULL CHECK (kind IN ('recommendation', 'over_threshold', 'substitution', 'online_order',
                     'checkout', 'pick_choice')),
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    amount_cents   INTEGER CHECK (amount_cents >= 0),
    sku            TEXT,
    rec_id         TEXT,
    options        JSONB NOT NULL DEFAULT '[]'::jsonb,       -- list[contracts.trust.ApprovalOption]
    multi          BOOLEAN NOT NULL DEFAULT FALSE,
    cart_hash      TEXT,
    expires_s      REAL NOT NULL DEFAULT 120,
    channels       TEXT[] NOT NULL DEFAULT '{web}',          -- ApprovalRequest.channels
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- response (NULL until resolved)
    decision          TEXT CHECK (decision IN ('approve', 'reject', 'timeout', 'choice')),
    choice            TEXT,
    choices           TEXT[] NOT NULL DEFAULT '{}',
    channel           TEXT CHECK (channel IN ('web', 'voice', 'photon', 'system')),
    response_user_id  TEXT,
    response_hash     TEXT,                                  -- ApprovalResponse.cart_hash
    note              TEXT,
    resolved_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS mandate_decisions (
    decision_id      TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users (user_id),
    trip_id          TEXT,
    mandate_id       TEXT,
    verdict          TEXT NOT NULL CHECK (verdict IN ('approve', 'needs_approval', 'deny')),
    reasons          TEXT[] NOT NULL DEFAULT '{}',
    messages         TEXT[] NOT NULL DEFAULT '{}',
    violations       JSONB NOT NULL DEFAULT '[]'::jsonb,     -- list[contracts.trust.MandateViolation]
    scope            TEXT NOT NULL DEFAULT 'cart' CHECK (scope IN ('cart', 'line')),
    sku              TEXT,
    total_cents      INTEGER,
    budget_cents     INTEGER,
    remaining_cents  INTEGER,
    cart_hash        TEXT,
    evaluated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id       TEXT PRIMARY KEY,
    trip_id          TEXT NOT NULL REFERENCES trips (trip_id),
    user_id          TEXT NOT NULL REFERENCES users (user_id),
    provider         TEXT NOT NULL CHECK (provider IN ('stripe_test', 'visa_sandbox', 'mock')),
    status           TEXT NOT NULL CHECK (status IN ('succeeded', 'declined', 'error')),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency         TEXT NOT NULL DEFAULT 'USD',
    in_store_cents   INTEGER NOT NULL DEFAULT 0,
    online_cents     INTEGER NOT NULL DEFAULT 0,
    lines            JSONB NOT NULL DEFAULT '[]'::jsonb,     -- list[contracts.shopping.CartLine]
    online_order_id  TEXT,
    provider_ref     TEXT,                                   -- Stripe pi_... (test mode)
    cart_hash        TEXT NOT NULL,
    mandate_id       TEXT,
    approval_id      TEXT,
    error            TEXT,
    signature        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only, hash-chained audit log (trust.AuditEntry).
CREATE TABLE IF NOT EXISTS audit_log (
    seq        BIGSERIAL PRIMARY KEY,
    entry_id   TEXT NOT NULL UNIQUE,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    trip_id    TEXT,
    user_id    TEXT,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash  TEXT,
    hash       TEXT,
    signature  TEXT,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_trip_idx ON audit_log (trip_id, seq);
