"""SQLite connection, schema, and upgrades for older databases."""

import sqlite3

from flask import g

from .config import DB_PATH
from .text import norm_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    steam_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE COLLATE NOCASE,
    igdb_id INTEGER,
    cover_id TEXT,
    release_year INTEGER,
    norm_title TEXT,
    igdb_checked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    rating REAL NOT NULL CHECK (rating >= 1.0 AND rating <= 10.0),
    review TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, game_id)
);
CREATE TABLE IF NOT EXISTS library (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    playtime_minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, game_id)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        cols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
        if "steam_id" not in cols:  # upgrade databases created before Steam support
            db.execute("ALTER TABLE users ADD COLUMN steam_id TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_steam ON users(steam_id)"
        )
        gcols = [r[1] for r in db.execute("PRAGMA table_info(games)")]
        for col, ddl in (
            ("igdb_id", "INTEGER"),
            ("cover_id", "TEXT"),
            ("release_year", "INTEGER"),
            ("norm_title", "TEXT"),
            ("igdb_checked", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in gcols:  # upgrade databases created before IGDB support
                db.execute(f"ALTER TABLE games ADD COLUMN {col} {ddl}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_games_igdb ON games(igdb_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_games_norm ON games(norm_title)")
        for gid, title in db.execute(
            "SELECT id, title FROM games WHERE norm_title IS NULL"
        ).fetchall():
            db.execute(
                "UPDATE games SET norm_title = ? WHERE id = ?", (norm_title(title), gid)
            )
