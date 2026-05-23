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
    avatar_url TEXT,

    -- Etterna-MSD-style skill profile (computed from top plays)
    -- Format: {"aim": 4.2, "speed": 3.8, "acc": 5.1, "stamina": 3.5, "flashlight": 2.0}
    skill_profile JSONB DEFAULT '{}'
);

-- Migration for existing tables (idempotent):
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS skill_profile JSONB DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS banner_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT '';

-- ── Web sessions (cookie-based auth for the website) ──────────────────────────
CREATE TABLE IF NOT EXISTS web_sessions (
    token TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),
    expires_at BIGINT NOT NULL,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);
ALTER TABLE web_sessions DISABLE ROW LEVEL SECURITY;

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
    last_decay_check BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),

    -- TRUE if the player paused during this play
    paused BOOLEAN DEFAULT FALSE
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
    vote_hate INTEGER DEFAULT 0,

    -- Cached per-axis skill demands (computed from metadata heuristics)
    -- Format: {"aim": 5.2, "speed": 4.1, "acc": 4.8, "stamina": 5.0, "flashlight": 2.5}
    skillset JSONB
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

-- Migration for existing beatmaps table:
ALTER TABLE beatmaps ADD COLUMN IF NOT EXISTS skillset JSONB;
ALTER TABLE beatmaps ADD COLUMN IF NOT EXISTS tags TEXT DEFAULT '';
ALTER TABLE scores ADD COLUMN IF NOT EXISTS paused BOOLEAN DEFAULT FALSE;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS per_column_acc JSONB;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS replay_path TEXT;

-- ── Dan Courses ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dan_courses (
    tier INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    -- Array of beatmap md5 hashes the player must pass in order
    maps JSONB NOT NULL DEFAULT '[]',
    -- Minimum accuracy required to pass each map (0-1 scale, e.g. 0.92)
    min_accuracy REAL DEFAULT 0.90,
    -- If TRUE, player must not pause during any map in the course
    no_pause BOOLEAN DEFAULT TRUE,
    created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())
);

-- ── Dan Progress (per user per course) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dan_progress (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    course_tier INTEGER NOT NULL REFERENCES dan_courses(tier),
    -- Array of md5s the player has completed in this attempt
    maps_completed JSONB DEFAULT '[]',
    passed BOOLEAN DEFAULT FALSE,
    started_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW()),
    completed_at BIGINT,
    UNIQUE(user_id, course_tier)
);

-- ── Seed some example Dan courses ─────────────────────────────────────────────
-- (Admins should replace these md5s with real maps via the admin panel)
INSERT INTO dan_courses (tier, name, description, maps, min_accuracy) VALUES
(1, '1st Dan', '4K beginner stream/JS fundamentals', '[]', 0.88),
(2, '2nd Dan', '4K intermediate streams', '[]', 0.90),
(3, '3rd Dan', '4K chordjack + handstream', '[]', 0.91),
(4, '4th Dan', '4K advanced speed + jacks', '[]', 0.92),
(5, '5th Dan', '4K expert all-rounder', '[]', 0.93),
(6, '6th Dan', '4K master streams + stamina', '[]', 0.94),
(7, '7th Dan', '4K high-level tech + speed', '[]', 0.95),
(8, '8th Dan', '4K elite all patterns', '[]', 0.95),
(9, '9th Dan', '4K near-Kaiden challenge', '[]', 0.96),
(10, 'Kaiden', '4K ultimate test — prove your mastery', '[]', 0.96)
ON CONFLICT (tier) DO NOTHING;

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
