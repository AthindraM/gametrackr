"""Reviews: likes and dislikes, comments, and the page for a single review."""

from flask import abort, flash, g, redirect, render_template, request, url_for

from . import app
from .activity import browser_timezone, when
from .db import get_db
from .helpers import login_required, safe_next
from .notify import clear_like, notify_comment, notify_like

COMMENTS_PER_PAGE = 50
MAX_COMMENT_LENGTH = 1000
MAX_COMMENTS_PER_MINUTE = 5  # a light brake on spam

# Like, dislike, and comment counts for entries aliased `e`. Contains one ? for the
# viewer's id, which fills in my_reaction (1 liked, -1 disliked, NULL neither).
REVIEW_STATS = """
    (SELECT COUNT(*) FROM review_reactions r WHERE r.entry_id = e.id AND r.value = 1) AS likes,
    (SELECT COUNT(*) FROM review_reactions r WHERE r.entry_id = e.id AND r.value = -1) AS dislikes,
    (SELECT COUNT(*) FROM review_comments c WHERE c.entry_id = e.id) AS comments,
    (SELECT r.value FROM review_reactions r WHERE r.entry_id = e.id AND r.user_id = ?) AS my_reaction"""


def get_review(db, entry_id):
    """A review (a rating that has text) with its counts, or a 404."""
    row = db.execute(
        f"""SELECT e.id, e.user_id, e.game_id, e.rating, e.review, e.updated_at,
                   u.username, u.avatar_v, g.title, g.cover_id, {REVIEW_STATS}
            FROM entries e
            JOIN users u ON u.id = e.user_id
            JOIN games g ON g.id = e.game_id
            WHERE e.id = ? AND e.review IS NOT NULL AND e.review != ''""",
        (g.user["id"] if g.user else -1, entry_id),
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/review/<int:entry_id>")
def review_page(entry_id):
    db = get_db()
    review = get_review(db, entry_id)
    pages = max(1, -(-review["comments"] // COMMENTS_PER_PAGE))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = db.execute(
        """SELECT c.id, c.user_id, c.body, c.created_at, u.username, u.avatar_v
           FROM review_comments c JOIN users u ON u.id = c.user_id
           WHERE c.entry_id = ?
           ORDER BY c.id LIMIT ? OFFSET ?""",
        (entry_id, COMMENTS_PER_PAGE, (page - 1) * COMMENTS_PER_PAGE),
    ).fetchall()
    tz, known_zone = browser_timezone()
    comments = [{**dict(r), "when": when(r["created_at"], tz)} for r in rows]
    query = request.query_string.decode()
    return render_template(
        "review.html",
        review=review,
        comments=comments,
        page=page,
        pages=pages,
        back_url=request.path + (f"?{query}" if query else ""),
        needs_tz=not known_zone,
    )


@app.route("/review/<int:entry_id>/react", methods=["POST"])
@login_required
def react_to_review(entry_id):
    """Like or dislike a review. Pressing the same button again takes it back."""
    value = {"like": 1, "dislike": -1}.get(request.form.get("value"))
    if value is None:
        abort(400)
    db = get_db()
    review = get_review(db, entry_id)
    back = safe_next(url_for("review_page", entry_id=entry_id))
    if review["user_id"] == g.user["id"]:
        flash("You can't like or dislike your own review.")
        return redirect(back)
    if review["my_reaction"] == value:
        db.execute(
            "DELETE FROM review_reactions WHERE entry_id = ? AND user_id = ?",
            (entry_id, g.user["id"]),
        )
        if value == 1:  # the like was taken back
            clear_like(db, g.user["id"], entry_id)
    else:
        db.execute(
            """INSERT INTO review_reactions (entry_id, user_id, value) VALUES (?, ?, ?)
               ON CONFLICT (entry_id, user_id) DO UPDATE SET
                 value = excluded.value, created_at = CURRENT_TIMESTAMP""",
            (entry_id, g.user["id"], value),
        )
        if value == 1:
            notify_like(db, review["user_id"], g.user["id"], entry_id)
        else:  # a dislike never notifies, and replaces any like that was there
            clear_like(db, g.user["id"], entry_id)
    db.commit()
    return redirect(back)


@app.route("/review/<int:entry_id>/comments", methods=["POST"])
@login_required
def add_comment(entry_id):
    db = get_db()
    review = get_review(db, entry_id)  # 404 unless this is a review
    body = request.form.get("body", "").replace("\r\n", "\n").strip()
    problem = None
    if not body:
        problem = "Write something first."
    elif len(body) > MAX_COMMENT_LENGTH:
        problem = f"Comments can be up to {MAX_COMMENT_LENGTH} characters."
    else:
        recent = db.execute(
            """SELECT COUNT(*) FROM review_comments
               WHERE user_id = ? AND created_at > datetime('now', '-1 minute')""",
            (g.user["id"],),
        ).fetchone()[0]
        if recent >= MAX_COMMENTS_PER_MINUTE:
            problem = "You're commenting too fast. Try again in a minute."
    if problem:
        flash(problem)
        return redirect(url_for("review_page", entry_id=entry_id))
    cur = db.execute(
        "INSERT INTO review_comments (entry_id, user_id, body) VALUES (?, ?, ?)",
        (entry_id, g.user["id"], body),
    )
    notify_comment(db, review["user_id"], g.user["id"], entry_id, cur.lastrowid)
    db.commit()
    total = db.execute(
        "SELECT COUNT(*) FROM review_comments WHERE entry_id = ?", (entry_id,)
    ).fetchone()[0]
    last_page = max(1, -(-total // COMMENTS_PER_PAGE))
    return redirect(
        url_for(
            "review_page",
            entry_id=entry_id,
            page=last_page if last_page > 1 else None,
            _anchor=f"comment-{cur.lastrowid}",
        )
    )


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    """A comment can be deleted by its author or by the owner of the review."""
    db = get_db()
    row = db.execute(
        """SELECT c.id, c.user_id, c.entry_id, e.user_id AS review_owner
           FROM review_comments c JOIN entries e ON e.id = c.entry_id
           WHERE c.id = ?""",
        (comment_id,),
    ).fetchone()
    if row is None:
        abort(404)
    if g.user["id"] not in (row["user_id"], row["review_owner"]):
        abort(403)
    db.execute("DELETE FROM review_comments WHERE id = ?", (comment_id,))
    db.commit()
    return redirect(safe_next(url_for("review_page", entry_id=row["entry_id"])))
