"""Searching for people by username."""


def find_users(db, q, limit=40):
    """Users whose username contains q: an exact match first, then names that start
    with q, then the most followed. Emails are never searched."""
    esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return db.execute(
        """SELECT u.id, u.username, u.avatar_v,
                  (SELECT COUNT(*) FROM entries e WHERE e.user_id = u.id) AS rated,
                  (SELECT COUNT(*) FROM follows f WHERE f.followee_id = u.id) AS followers
           FROM users u
           WHERE u.username LIKE ? ESCAPE '!'
           ORDER BY (u.username = ? COLLATE NOCASE) DESC,
                    (u.username LIKE ? ESCAPE '!') DESC,
                    followers DESC, rated DESC, u.username COLLATE NOCASE
           LIMIT ?""",
        (f"%{esc}%", q, f"{esc}%", limit),
    ).fetchall()
