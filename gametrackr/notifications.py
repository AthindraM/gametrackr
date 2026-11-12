"""The Notifications page."""

from datetime import datetime

from flask import flash, g, redirect, render_template, request, url_for

from . import app
from .activity import UTC, browser_timezone, clock, day_label, group_by_day
from .db import get_db
from .follows import following_among
from .helpers import login_required
from .notify import PREFERENCES, VISIBLE, save_prefs, unread_count
from .reviews import COMMENTS_PER_PAGE

PAGE_SIZE = 30
KEEP_READ_DAYS = 90  # read notifications older than this are cleaned up


@app.route("/notifications")
@login_required
def notifications_page():
    db = get_db()
    uid = g.user["id"]
    db.execute(
        """DELETE FROM notifications
           WHERE user_id = ? AND read_at IS NOT NULL
             AND read_at < datetime('now', ?)""",
        (uid, f"-{KEEP_READ_DAYS} days"),
    )
    total = db.execute(
        f"""SELECT COUNT(*) FROM notifications n
            LEFT JOIN entries e ON e.id = n.entry_id
            WHERE n.user_id = ? AND {VISIBLE}""",
        (uid,),
    ).fetchone()[0]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = db.execute(
        f"""SELECT n.id, n.kind, n.created_at, n.read_at, n.actor_id, n.entry_id, n.comment_id,
                   a.username, a.avatar_v, e.game_id, e.user_id AS owner_id,
                   ou.username AS owner, g.title, c.body
            FROM notifications n
            JOIN users a ON a.id = n.actor_id
            LEFT JOIN entries e ON e.id = n.entry_id
            LEFT JOIN users ou ON ou.id = e.user_id
            LEFT JOIN games g ON g.id = e.game_id
            LEFT JOIN review_comments c ON c.id = n.comment_id
            WHERE n.user_id = ? AND {VISIBLE}
            ORDER BY n.created_at DESC, n.id DESC LIMIT ? OFFSET ?""",
        (uid, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()

    tz, known_zone = browser_timezone()
    today = datetime.now(UTC).astimezone(tz).date()
    items = []
    for row in rows:
        item = dict(row)
        local = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        local = local.astimezone(tz)
        item.update(
            day=local.date(),
            day_label=day_label(local.date(), today),
            time=clock(local),
            page=None,
        )
        if row["kind"] == "comment":  # link to the page of the thread the comment is on
            position = db.execute(
                "SELECT COUNT(*) FROM review_comments WHERE entry_id = ? AND id <= ?",
                (row["entry_id"], row["comment_id"]),
            ).fetchone()[0]
            comment_page = max(1, -(-position // COMMENTS_PER_PAGE))
            item["page"] = comment_page if comment_page > 1 else None
        items.append(item)

    following_ids = following_among(
        db, uid, [i["actor_id"] for i in items if i["kind"] == "follow"]
    )
    fresh = [i["id"] for i in items if i["read_at"] is None]
    if (
        fresh
    ):  # viewing them marks them as read (the page below still shows them as new)
        marks = ",".join("?" * len(fresh))
        db.execute(
            f"UPDATE notifications SET read_at = CURRENT_TIMESTAMP WHERE id IN ({marks})",
            fresh,
        )
    db.commit()
    query = request.query_string.decode()
    return render_template(
        "notifications.html",
        sections=group_by_day(items),
        page=page,
        pages=pages,
        more_unread=unread_count(db, uid),
        following_ids=following_ids,
        next_url=request.path + (f"?{query}" if query else ""),
        needs_tz=not known_zone,
    )


@app.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    db = get_db()
    db.execute(
        "UPDATE notifications SET read_at = CURRENT_TIMESTAMP WHERE user_id = ? AND read_at IS NULL",
        (g.user["id"],),
    )
    db.commit()
    return redirect(url_for("notifications_page"))


@app.route("/settings/notifications", methods=["POST"])
@login_required
def save_notification_prefs():
    db = get_db()
    # An unticked box is simply missing from the form, so each switch is "present or not".
    save_prefs(db, g.user["id"], {key: key in request.form for key, _ in PREFERENCES})
    db.commit()
    flash("Notification settings saved.")
    return redirect(url_for("settings", section="notifications"))
