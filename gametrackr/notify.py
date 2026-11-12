"""Creating and counting notifications. (The page that lists them is notifications.py.)

You are notified when someone likes or comments on your review, or follows you.
You are also told about new comments on a review you have commented on.
Dislikes and unfollows never notify, and taking a like or a follow back removes the
notification it made, so a withdrawn like can't linger. Each kind can be switched off
in Settings (everything is on until you change it).
"""

# The switches in Settings > Notifications, in order. Unset means on.
PREFERENCES = [
    ("like", "Likes on my reviews"),
    ("comment", "Comments on my reviews"),
    ("thread", "New comments on reviews I've commented on"),
    ("follow", "New followers"),
]
MAX_THREAD_RECIPIENTS = 50  # most people told about one comment in a long thread

# A notification is shown unless it points at a review that no longer has any text.
VISIBLE = "(n.kind = 'follow' OR (e.review IS NOT NULL AND e.review != ''))"


def unread_count(db, user_id):
    return db.execute(
        f"""SELECT COUNT(*) FROM notifications n
            LEFT JOIN entries e ON e.id = n.entry_id
            WHERE n.user_id = ? AND n.read_at IS NULL AND {VISIBLE}""",
        (user_id,),
    ).fetchone()[0]


def notify_like(db, owner_id, actor_id, entry_id):
    if not enabled(db, owner_id, "like"):
        return
    # The unique index keeps this to one notification per person per review.
    db.execute(
        """INSERT OR IGNORE INTO notifications (user_id, actor_id, kind, entry_id)
           VALUES (?, ?, 'like', ?)""",
        (owner_id, actor_id, entry_id),
    )


def clear_like(db, actor_id, entry_id):
    db.execute(
        "DELETE FROM notifications WHERE kind = 'like' AND actor_id = ? AND entry_id = ?",
        (actor_id, entry_id),
    )


def notify_comment(db, owner_id, actor_id, entry_id, comment_id):
    """Tell the review's owner about a new comment, and also anyone who commented earlier.

    The commenter is never told about their own comment. Someone who is already
    waiting on an unread comment notification for this review isn't sent another.
    """
    add = """INSERT INTO notifications (user_id, actor_id, kind, entry_id, comment_id)
             VALUES (?, ?, 'comment', ?, ?)"""
    if actor_id != owner_id and enabled(db, owner_id, "comment"):
        db.execute(add, (owner_id, actor_id, entry_id, comment_id))
    earlier = db.execute(
        """SELECT DISTINCT user_id FROM review_comments
           WHERE entry_id = ? AND user_id NOT IN (?, ?) LIMIT ?""",
        (entry_id, actor_id, owner_id, MAX_THREAD_RECIPIENTS),
    ).fetchall()
    for row in earlier:
        waiting = db.execute(
            """SELECT 1 FROM notifications
               WHERE user_id = ? AND entry_id = ? AND kind = 'comment' AND read_at IS NULL""",
            (row["user_id"], entry_id),
        ).fetchone()
        if not waiting and enabled(db, row["user_id"], "thread"):
            db.execute(add, (row["user_id"], actor_id, entry_id, comment_id))


def notify_follow(db, user_id, actor_id):
    if not enabled(db, user_id, "follow"):
        return
    db.execute(
        "INSERT OR IGNORE INTO notifications (user_id, actor_id, kind) VALUES (?, ?, 'follow')",
        (user_id, actor_id),
    )


def clear_follow(db, user_id, actor_id):
    db.execute(
        "DELETE FROM notifications WHERE kind = 'follow' AND user_id = ? AND actor_id = ?",
        (user_id, actor_id),
    )


def get_prefs(db, user_id):
    """{"like": True, ...}: which kinds this user wants (all on by default)."""
    prefs = {key: True for key, _ in PREFERENCES}
    for row in db.execute(
        "SELECT pref, enabled FROM notification_prefs WHERE user_id = ?", (user_id,)
    ):
        if row["pref"] in prefs:
            prefs[row["pref"]] = bool(row["enabled"])
    return prefs


def enabled(db, user_id, pref):
    row = db.execute(
        "SELECT enabled FROM notification_prefs WHERE user_id = ? AND pref = ?",
        (user_id, pref),
    ).fetchone()
    return row is None or bool(row["enabled"])


def save_prefs(db, user_id, flags):
    """Store the switches; `flags` maps each preference to True or False."""
    for key, _ in PREFERENCES:
        db.execute(
            """INSERT INTO notification_prefs (user_id, pref, enabled) VALUES (?, ?, ?)
               ON CONFLICT (user_id, pref) DO UPDATE SET enabled = excluded.enabled""",
            (user_id, key, 1 if flags.get(key) else 0),
        )
