"""Following other users: follow/unfollow, and the followers/following lists."""

from flask import flash, g, redirect, render_template, request, url_for

from . import app
from .db import get_db
from .helpers import find_person, login_required

PAGE_SIZE = 50


def follower_counts(db, user_id):
    """(how many follow this user, how many this user follows)."""
    followers = db.execute(
        "SELECT COUNT(*) FROM follows WHERE followee_id = ?", (user_id,)
    ).fetchone()[0]
    following = db.execute(
        "SELECT COUNT(*) FROM follows WHERE follower_id = ?", (user_id,)
    ).fetchone()[0]
    return followers, following


def is_following(db, follower_id, followee_id):
    return (
        db.execute(
            "SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?",
            (follower_id, followee_id),
        ).fetchone()
        is not None
    )


def safe_next(default):
    """Where to go after a button press: the page it came from, if it's on this site."""
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


@app.route("/u/<username>/follow", methods=["POST"])
@login_required
def follow_user(username):
    db = get_db()
    person = find_person(db, username)
    fallback = url_for("profile", username=person["username"])
    if person["id"] == g.user["id"]:
        flash("You can't follow yourself.")
        return redirect(safe_next(fallback))
    db.execute(
        "INSERT OR IGNORE INTO follows (follower_id, followee_id) VALUES (?, ?)",
        (g.user["id"], person["id"]),
    )
    db.commit()
    flash(f"You're now following {person['username']}.")
    return redirect(safe_next(fallback))


@app.route("/u/<username>/unfollow", methods=["POST"])
@login_required
def unfollow_user(username):
    db = get_db()
    person = find_person(db, username)
    db.execute(
        "DELETE FROM follows WHERE follower_id = ? AND followee_id = ?",
        (g.user["id"], person["id"]),
    )
    db.commit()
    flash(f"You unfollowed {person['username']}.")
    return redirect(safe_next(url_for("profile", username=person["username"])))


def people_page(username, kind):
    db = get_db()
    person = find_person(db, username)
    # followers: people whose follow points at this user; following: the reverse
    if kind == "followers":
        shown, anchor = "follower_id", "followee_id"
    else:
        shown, anchor = "followee_id", "follower_id"
    total = db.execute(
        f"SELECT COUNT(*) FROM follows WHERE {anchor} = ?", (person["id"],)
    ).fetchone()[0]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    people = db.execute(
        f"""SELECT u.id, u.username, u.avatar_v,
                   (SELECT COUNT(*) FROM entries e WHERE e.user_id = u.id) AS rated
            FROM follows f JOIN users u ON u.id = f.{shown}
            WHERE f.{anchor} = ?
            ORDER BY f.created_at DESC, u.username
            LIMIT ? OFFSET ?""",
        (person["id"], PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    following_ids = set()
    if g.user and people:
        marks = ",".join("?" * len(people))
        rows = db.execute(
            f"SELECT followee_id FROM follows WHERE follower_id = ? AND followee_id IN ({marks})",
            [g.user["id"]] + [p["id"] for p in people],
        ).fetchall()
        following_ids = {r[0] for r in rows}
    query = request.query_string.decode()
    return render_template(
        "people.html",
        person=person,
        title="Followers" if kind == "followers" else "Following",
        empty=(
            "No followers yet." if kind == "followers" else "Not following anyone yet."
        ),
        endpoint=f"{kind}_page",
        people=people,
        total=total,
        page=page,
        pages=pages,
        following_ids=following_ids,
        next_url=request.path + (f"?{query}" if query else ""),
    )


@app.route("/u/<username>/followers")
def followers_page(username):
    return people_page(username, "followers")


@app.route("/u/<username>/following")
def following_page(username):
    return people_page(username, "following")
