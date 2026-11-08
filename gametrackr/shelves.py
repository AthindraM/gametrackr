"""Shelves (Currently Playing / Completed / Wishlist) and the queries behind profile shelves."""

# Shelves a user can put a game on (the dropdown on game pages and the Add game page).
SHELVES = {
    "playing": "Currently Playing",
    "completed": "Completed",
    "wishlist": "Wishlist",
}

# Shelves shown on a profile, in order. "top-rated" and "library" are built from
# ratings and everything the user has, rather than chosen from the dropdown.
PROFILE_SHELVES = [
    ("top-rated", "Top Rated"),
    ("playing", "Currently Playing"),
    ("wishlist", "Wishlist"),
    ("completed", "Completed"),
    ("library", "Library"),
]
PROFILE_SHELF_NAMES = dict(PROFILE_SHELVES)

PREVIEW_COUNT = 4  # games shown per shelf on the profile
TOP_RATED_PAGE_LIMIT = 10  # games shown when Top Rated is expanded

# Every game the user has rated, shelved, or imported from Steam.
_LIBRARY_WHERE = """g.id IN (SELECT game_id FROM entries WHERE user_id = ?)
                 OR g.id IN (SELECT game_id FROM shelf_items WHERE user_id = ?)
                 OR g.id IN (SELECT game_id FROM library WHERE user_id = ?)"""


def get_shelf(db, user_id, game_id):
    """The shelf key a user put this game on, or "" if none."""
    row = db.execute(
        "SELECT shelf FROM shelf_items WHERE user_id = ? AND game_id = ?",
        (user_id, game_id),
    ).fetchone()
    return row["shelf"] if row else ""


def put_on_shelf(db, user_id, game_id, shelf):
    """Put a game on a shelf, or take it off all shelves when shelf is empty."""
    if not shelf:
        db.execute(
            "DELETE FROM shelf_items WHERE user_id = ? AND game_id = ?",
            (user_id, game_id),
        )
        return
    db.execute(
        """INSERT INTO shelf_items (user_id, game_id, shelf) VALUES (?, ?, ?)
           ON CONFLICT (user_id, game_id) DO UPDATE SET
             shelf = excluded.shelf, added_at = CURRENT_TIMESTAMP
           WHERE shelf != excluded.shelf""",
        (user_id, game_id, shelf),
    )


def shelf_games(db, user_id, key, limit=None):
    """Games on a profile shelf, each with the user's own rating (None if unrated)."""
    cap = limit if limit else -1  # SQLite: LIMIT -1 means no limit
    if key == "top-rated":
        return db.execute(
            """SELECT g.id AS game_id, g.title, g.cover_id, e.rating
               FROM entries e JOIN games g ON g.id = e.game_id
               WHERE e.user_id = ?
               ORDER BY e.rating DESC, e.updated_at DESC, e.id DESC LIMIT ?""",
            (user_id, cap),
        ).fetchall()
    if key == "library":
        return db.execute(
            f"""SELECT g.id AS game_id, g.title, g.cover_id, e.rating
                FROM games g
                LEFT JOIN entries e ON e.game_id = g.id AND e.user_id = ?
                WHERE {_LIBRARY_WHERE}
                ORDER BY g.title COLLATE NOCASE LIMIT ?""",
            (user_id,) * 4 + (cap,),
        ).fetchall()
    return db.execute(
        """SELECT g.id AS game_id, g.title, g.cover_id, e.rating
           FROM shelf_items s
           JOIN games g ON g.id = s.game_id
           LEFT JOIN entries e ON e.game_id = s.game_id AND e.user_id = s.user_id
           WHERE s.user_id = ? AND s.shelf = ?
           ORDER BY s.added_at DESC, s.game_id DESC LIMIT ?""",
        (user_id, key, cap),
    ).fetchall()


def shelf_total(db, user_id, key):
    if key == "top-rated":
        sql, args = "SELECT COUNT(*) FROM entries WHERE user_id = ?", (user_id,)
    elif key == "library":
        sql = f"SELECT COUNT(*) FROM games g WHERE {_LIBRARY_WHERE}"
        args = (user_id,) * 3
    else:
        sql = "SELECT COUNT(*) FROM shelf_items WHERE user_id = ? AND shelf = ?"
        args = (user_id, key)
    return db.execute(sql, args).fetchone()[0]
