"""The home page: Home and Following tabs, and the site-wide Top Rated ranking."""

from flask import g, redirect, render_template, request, url_for

from . import app
from .activity import (
    FEED_PAGE_SIZE,
    FOLLOWED,
    browser_timezone,
    count,
    decorate,
    fetch,
    group_by_day,
)
from .db import get_db
from .helpers import login_required
from .shelves import PREVIEW_COUNT

TOP_LIMIT = 100  # games in the site-wide Top Rated ranking


def top_games(db, limit=TOP_LIMIT):
    """The highest-rated games across all users (average rating; more ratings break ties)."""
    return db.execute(
        """SELECT g.id AS game_id, g.title, g.cover_id, g.release_year AS year,
                  AVG(e.rating) AS avg, COUNT(*) AS n
           FROM games g JOIN entries e ON e.game_id = g.id
           GROUP BY g.id
           ORDER BY avg DESC, n DESC, g.title COLLATE NOCASE
           LIMIT ?""",
        (limit,),
    ).fetchall()


@app.route("/")
def home():
    if g.user is None:
        return render_template("landing.html")
    top = top_games(get_db())
    return render_template(
        "home.html", tab="home", games=top[:PREVIEW_COUNT], total=len(top)
    )


@app.route("/top-rated")
def top_rated():
    rows = []
    for rank, game in enumerate(top_games(get_db()), 1):
        sub = f"{game['n']} rating{'' if game['n'] == 1 else 's'}"
        if game["year"]:
            sub += f" \u00b7 {game['year']}"
        rows.append(
            {
                "rank": rank,
                "href": url_for("game", game_id=game["game_id"]),
                "title": game["title"],
                "cover_id": game["cover_id"],
                "rating": game["avg"],
                "sub": sub,
            }
        )
    return render_template(
        "ranking.html",
        title="Top Rated",
        subtitle=f"Highest average rating across all users \u00b7 {len(rows)} games",
        back={"url": url_for("home"), "label": "Home"},
        rows=rows,
    )


@app.route("/following")
@login_required
def following_feed():
    db = get_db()
    uid = g.user["id"]
    tz, known_zone = browser_timezone()
    following = db.execute(
        "SELECT COUNT(*) FROM follows WHERE follower_id = ?", (uid,)
    ).fetchone()[0]
    total = count(db, FOLLOWED, [uid])
    pages = max(1, -(-total // FEED_PAGE_SIZE))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = fetch(db, FOLLOWED, [uid], FEED_PAGE_SIZE, (page - 1) * FEED_PAGE_SIZE)
    return render_template(
        "following.html",
        tab="following",
        sections=group_by_day(decorate(rows, tz)),
        following=following,
        page=page,
        pages=pages,
        needs_tz=not known_zone,
    )
