-- Scythe Private Server — Supabase Schema
-- Paste this entire file into Supabase SQL Editor and run it.

-- ── Users ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    username_safe TEXT UNIQUE NOT NULL,
    password_md5 TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    registered_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),
    last_seen BIGINT DEFAULT 0,

    -- 0=normal, 1=restricted, 2=shadowbanned, 3=admin
    status INTEGER DEFAULT 0,

    ranked_score BIGINT DEFAULT 0,
    total_score BIGINT DEFAULT 0,
    playcount INTEGER DEFAULT 0,
    pp REAL DEFAULT 0.0,
    accuracy REAL DEFAULT 0.0,
    max_combo INTEGER DEFAULT 0,
    rank INTEGER DEFAULT 0,

    hot_streak INTEGER DEFAULT 0,
    clips JSONB DEFAULT '[]',
    stalker_mode BOOLEAN DEFAULT FALSE,
    friends JSONB DEFAULT '[]',
    country TEXT DEFAULT 'XX',

    -- per-user avatar URL; if NULL, the avatar handler falls back to a
    -- DiceBear pixel-art avatar seeded by user id (so every player has a
    -- unique custom-looking pic the moment they log in).
    avatar_url TEXT
);

-- Migration for existing tables (idempotent):
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;

-- ── Scores ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scores (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    beatmap_md5 TEXT NOT NULL,
    score BIGINT NOT NULL,
    pp REAL DEFAULT 0.0,
    accuracy REAL NOT NULL,
    max_combo INTEGER NOT NULL,
    count300 INTEGER DEFAULT 0,
    count100 INTEGER DEFAULT 0,
    count50 INTEGER DEFAULT 0,
    countmiss INTEGER DEFAULT 0,
    mods INTEGER DEFAULT 0,
    rank TEXT DEFAULT 'F',
    is_fc BOOLEAN DEFAULT FALSE,
    passed BOOLEAN DEFAULT TRUE,
    submitted_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),

    ac_flagged BOOLEAN DEFAULT FALSE,
    ac_flag_reason TEXT DEFAULT '',
    ac_reviewed BOOLEAN DEFAULT FALSE,
    pp_decayed REAL DEFAULT 0.0,
    last_decay_check BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())
);

-- ── Beatmaps ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS beatmaps (
    md5 TEXT PRIMARY KEY,
    beatmap_id INTEGER DEFAULT 0,
    beatmapset_id INTEGER DEFAULT 0,
    title TEXT DEFAULT '',
    artist TEXT DEFAULT '',
    version TEXT DEFAULT '',
    creator TEXT DEFAULT '',
    status INTEGER DEFAULT 5,  -- 5=loved, forces in-game leaderboard
    ar REAL DEFAULT 9.0,
    od REAL DEFAULT 8.0,
    cs REAL DEFAULT 4.0,
    hp REAL DEFAULT 6.0,
    bpm REAL DEFAULT 180.0,
    total_length INTEGER DEFAULT 0,
    max_combo INTEGER DEFAULT 0,
    diff_rating REAL DEFAULT 3.0,
    last_update BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),
    vote_love INTEGER DEFAULT 0,
    vote_hate INTEGER DEFAULT 0
);

-- ── AC Log ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ac_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    score_id BIGINT,
    flag_type TEXT NOT NULL,
    flag_detail TEXT DEFAULT '',
    flagged_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),
    action_taken TEXT DEFAULT 'shadowban'
);

-- ── Map votes ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS map_votes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    beatmap_md5 TEXT NOT NULL,
    vote INTEGER NOT NULL,
    voted_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),
    UNIQUE(user_id, beatmap_md5)
);

-- ── Featured map ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS featured_map (
    id INTEGER PRIMARY KEY DEFAULT 1,
    beatmap_md5 TEXT,
    date TEXT,
    set_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_scores_beatmap ON scores(beatmap_md5);
CREATE INDEX IF NOT EXISTS idx_scores_user ON scores(user_id);
CREATE INDEX IF NOT EXISTS idx_scores_pp ON scores(pp DESC);
CREATE INDEX IF NOT EXISTS idx_scores_passed ON scores(passed);
CREATE INDEX IF NOT EXISTS idx_users_pp ON users(pp DESC);

-- ── Default admin user ────────────────────────────────────────────────────────
-- Password is MD5 of "changeme123" — CHANGE THIS
INSERT INTO users (username, username_safe, password_md5, email, status)
VALUES ('Scythe', 'scythe', '2a1d6f37d1832e076e63c1bad1c7cfca', 'admin@scythe.gg', 3)
ON CONFLICT DO NOTHING;

-- ── Row Level Security (optional but recommended) ─────────────────────────────
-- Disable RLS for server-side access (we use service role key)
ALTER TABLE users DISABLE ROW LEVEL SECURITY;
ALTER TABLE scores DISABLE ROW LEVEL SECURITY;
ALTER TABLE beatmaps DISABLE ROW LEVEL SECURITY;
ALTER TABLE ac_log DISABLE ROW LEVEL SECURITY;
ALTER TABLE map_votes DISABLE ROW LEVEL SECURITY;
ALTER TABLE featured_map DISABLE ROW LEVEL SECURITY;
