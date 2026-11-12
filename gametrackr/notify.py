"""Creating and counting notifications. (The page that lists them is notifications.py.)

You are notified when someone likes or comments on your review, or follows you.
Dislikes and unfollows never notify, and taking a like or a follow back removes the
notification it made, so a withdrawn like can't linger.
"""

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
    db.execute(
        """INSERT INTO notifications (user_id, actor_id, kind, entry_id, comment_id)
           VALUES (?, ?, 'comment', ?, ?)""",
        (owner_id, actor_id, entry_id, comment_id),
    )


def notify_follow(db, user_id, actor_id):
    db.execute(
        "INSERT OR IGNORE INTO notifications (user_id, actor_id, kind) VALUES (?, ?, 'follow')",
        (user_id, actor_id),
    )


def clear_follow(db, user_id, actor_id):
    db.execute(
        "DELETE FROM notifications WHERE kind = 'follow' AND user_id = ? AND actor_id = ?",
        (user_id, actor_id),
    )
