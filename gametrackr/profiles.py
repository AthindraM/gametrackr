"""Profile pages: shelves of games, and the "View all" page for each shelf."""

from flask import abort, g, render_template, url_for

from . import app
from .activity import (
    ONE_USER,
    PROFILE_ACTIVITY_COUNT,
    browser_timezone,
    decorate,
    fetch,
)
from .catalog import enrich_game
from .db import get_db
from .follows import follower_counts, is_following
from .helpers import find_person
from .igdb import igdb_configured
from .shelves import (
    PREVIEW_COUNT,
    PROFILE_SHELF_NAMES,
    PROFILE_SHELVES,
    TOP_RATED_PAGE_LIMIT,
    shelf_games,
    shelf_total,
)


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
    followers, following = follower_counts(db, uid)
    tz, known_zone = browser_timezone()
    activity = decorate(fetch(db, ONE_USER, [uid], PROFILE_ACTIVITY_COUNT), tz)
    return render_template(
        "profile.html",
        activity=activity,
        needs_tz=not known_zone,
        person=person,
        rated=stats["n"],
        avg=stats["avg"],
        shelves=shelves,
        followers=followers,
        following=following,
        viewer_follows=bool(g.user) and is_following(db, g.user["id"], uid),
    )


@app.route("/u/<username>/shelf/<key>")
def shelf_page(username, key):
    if key not in PROFILE_SHELF_NAMES:
        abort(404)
    db = get_db()
    person = find_person(db, username)
    limit = TOP_RATED_PAGE_LIMIT if key == "top-rated" else None
    games = shelf_games(db, person["id"], key, limit)
    total = shelf_total(db, person["id"], key)
    if key == "top-rated":  # the ranking: a numbered list, best first
        rows = [
            {
                "rank": rank,
                "href": url_for("game", game_id=game["game_id"]),
                "title": game["title"],
                "cover_id": game["cover_id"],
                "rating": game["rating"],
                "sub": None,
            }
            for rank, game in enumerate(games, 1)
        ]
        shown = (
            f"Top {limit} of {total} rated games"
            if total > limit
            else f"{total} rated games"
        )
        return render_template(
            "ranking.html",
            title=f"{person['username']}'s Top Rated",
            subtitle=shown,
            back={
                "url": url_for("profile", username=person["username"]),
                "label": person["username"],
            },
            rows=rows,
        )
    return render_template(
        "shelf.html",
        person=person,
        name=PROFILE_SHELF_NAMES[key],
        games=games,
        total=total,
    )
