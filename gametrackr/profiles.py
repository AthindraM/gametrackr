"""Profile pages: shelves of games, and the "View all" page for each shelf."""

from flask import abort, g, render_template

from . import app
from .catalog import enrich_game
from .db import get_db
from .igdb import igdb_configured
from .shelves import (
    PREVIEW_COUNT,
    PROFILE_SHELF_NAMES,
    PROFILE_SHELVES,
    TOP_RATED_PAGE_LIMIT,
    shelf_games,
    shelf_total,
)


def find_person(db, username):
    person = db.execute(
        "SELECT id, username, created_at FROM users WHERE username = ?", (username,)
    ).fetchone()
    if person is None:
        abort(404)
    return person


@app.route("/u/<username>")
def profile(username):
    db = get_db()
    person = find_person(db, username)
    uid = person["id"]
    if igdb_configured() and g.user and g.user["id"] == uid:
        # Fetch cover art for a few unmatched games per visit (most-played first).
        pending = db.execute(
            """SELECT g.id FROM games g
               WHERE g.igdb_id IS NULL AND g.igdb_checked = 0
                 AND (g.id IN (SELECT game_id FROM library WHERE user_id = ?)
                      OR g.id IN (SELECT game_id FROM entries WHERE user_id = ?)
                      OR g.id IN (SELECT game_id FROM shelf_items WHERE user_id = ?))
               ORDER BY COALESCE((SELECT playtime_minutes FROM library l
                                  WHERE l.game_id = g.id AND l.user_id = ?), 0) DESC
               LIMIT 3""",
            (uid,) * 4,
        ).fetchall()
        for r in pending:
            enrich_game(db, r["id"])
    stats = db.execute(
        "SELECT COUNT(*) AS n, AVG(rating) AS avg FROM entries WHERE user_id = ?",
        (uid,),
    ).fetchone()
    shelves = [
        {
            "key": key,
            "name": name,
            "total": shelf_total(db, uid, key),
            "games": shelf_games(db, uid, key, PREVIEW_COUNT),
        }
        for key, name in PROFILE_SHELVES
    ]
    return render_template(
        "profile.html",
        person=person,
        rated=stats["n"],
        avg=stats["avg"],
        shelves=shelves,
    )


@app.route("/u/<username>/shelf/<key>")
def shelf_page(username, key):
    if key not in PROFILE_SHELF_NAMES:
        abort(404)
    db = get_db()
    person = find_person(db, username)
    limit = TOP_RATED_PAGE_LIMIT if key == "top-rated" else None
    return render_template(
        "shelf.html",
        person=person,
        name=PROFILE_SHELF_NAMES[key],
        games=shelf_games(db, person["id"], key, limit),
        total=shelf_total(db, person["id"], key),
        limit=limit,
    )
