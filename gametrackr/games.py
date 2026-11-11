"""Home page, search, game pages, profiles, and adding/removing ratings."""

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

from . import app
from .catalog import enrich_game, ensure_game_from_igdb, search_results
from .db import get_db
from .follows import following_among
from .helpers import login_required
from .igdb import IgdbError, cover_url, igdb_configured, igdb_get
from .ranking import SORTS, sort_results
from .reviews import REVIEW_STATS
from .shelves import SHELVES, get_shelf, put_on_shelf
from .text import norm_title
from .users import find_users


@app.route("/api/games/suggest")
def suggest_games():
    q = " ".join(request.args.get("q", "").split())[:100]
    if not q:
        return jsonify([])
    return jsonify(
        [
            {
                "id": r["id"],
                "igdb_id": r["igdb_id"],
                "title": r["title"],
                "year": r["year"],
                "cover_url": cover_url(r["cover_id"]),
                "avg": r["avg"],
                "n": r["n"],
            }
            for r in search_results(q, 8, 15)[:8]
        ]
    )


@app.route("/search")
def search():
    q = " ".join(request.args.get("q", "").split())[:100]
    if request.args.get("type") == "users":
        db = get_db()
        users = find_users(db, q) if q else []
        following_ids = (
            following_among(db, g.user["id"], [u["id"] for u in users])
            if g.user
            else set()
        )
        query = request.query_string.decode()
        return render_template(
            "search.html",
            q=q,
            tab="users",
            users=users,
            following_ids=following_ids,
            next_url=request.path + (f"?{query}" if query else ""),
        )
    sort = request.args.get("sort", "relevance")
    if sort not in SORTS:
        sort = "relevance"
    # Keep the 40 best matches, then order them; sorting by date never pulls in poor matches.
    results = sort_results(search_results(q, 50, 30)[:40], sort) if q else []
    return render_template(
        "search.html", q=q, tab="games", results=results, sort=sort, sorts=SORTS
    )


@app.route("/game/igdb/<int:igdb_id>")
def game_from_igdb(igdb_id):
    db = get_db()
    row = db.execute("SELECT id FROM games WHERE igdb_id = ?", (igdb_id,)).fetchone()
    if row is None:
        if not igdb_configured():
            abort(404)
        try:
            ig = igdb_get(igdb_id)
            if ig is None:
                abort(404)
            gid = ensure_game_from_igdb(db, ig)
            db.commit()
        except IgdbError:
            flash("Couldn't reach IGDB right now. Try again in a moment.")
            return redirect(url_for("home"))
    else:
        gid = row["id"]
    return redirect(url_for("game", game_id=gid))


@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    db = get_db()
    form = {"title": "", "rating": "", "review": "", "shelf": "", "shelf_was": ""}
    editing = False
    locked = False

    game_id = request.args.get("game", type=int)
    if request.method == "GET" and game_id:
        row = db.execute(
            """SELECT g.title, e.rating, e.review, s.shelf FROM games g
               LEFT JOIN entries e ON e.game_id = g.id AND e.user_id = ?
               LEFT JOIN shelf_items s ON s.game_id = g.id AND s.user_id = ?
               WHERE g.id = ?""",
            (g.user["id"], g.user["id"], game_id),
        ).fetchone()
        if row:
            form["title"] = row["title"]
            form["shelf"] = form["shelf_was"] = row["shelf"] or ""
            locked = True
            if row["rating"] is not None:
                form["rating"] = f"{row['rating']:.1f}"
                form["review"] = row["review"] or ""
                editing = True

    if request.method == "GET" and not game_id:
        form["title"] = " ".join(request.args.get("title", "").split())[:120]

    if request.method == "POST":
        title = " ".join(request.form.get("title", "").split())
        review = request.form.get("review", "").strip()
        raw_rating = request.form.get("rating", "").strip()
        shelf = request.form.get("shelf", "")
        shelf_was = request.form.get(
            "shelf_was", ""
        )  # what the page showed when it loaded
        form = {
            "title": title,
            "rating": raw_rating,
            "review": review,
            "igdb_id": request.form.get("igdb_id", ""),
            "shelf": shelf,
            "shelf_was": shelf_was,
        }
        error = None
        rating = None
        if raw_rating:
            try:
                rating = round(float(raw_rating), 1)
                if not 1.0 <= rating <= 10.0:
                    raise ValueError
            except ValueError:
                error = "Rating must be a number from 1.0 to 10.0."
        elif not shelf:
            error = "Enter a rating, or pick a shelf."
        if shelf and shelf not in SHELVES:
            error = "Pick a shelf from the list."
        if not title or len(title) > 120:
            error = "Enter a game title (up to 120 characters)."
        elif len(review) > 5000:
            error = "Review is too long (5000 characters max)."
        elif not error and review and rating is None:
            error = "Add a rating to save a review."

        igdb_id = request.form.get("igdb_id", type=int)
        gid = None
        if not error and igdb_id and igdb_configured():
            try:
                ig = igdb_get(igdb_id)
                if ig is None:
                    raise IgdbError("Game not found on IGDB.")
                gid = ensure_game_from_igdb(db, ig)
            except IgdbError:
                error = "Couldn't reach IGDB right now. Try again in a moment."
        if error:
            flash(error)
        else:
            if gid is None:
                db.execute(
                    "INSERT OR IGNORE INTO games (title, norm_title) VALUES (?, ?)",
                    (title, norm_title(title)),
                )
                gid = db.execute(
                    "SELECT id FROM games WHERE title = ?", (title,)
                ).fetchone()["id"]
            if rating is not None:
                db.execute(
                    """INSERT INTO entries (user_id, game_id, rating, review)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (user_id, game_id) DO UPDATE SET
                         rating = excluded.rating,
                         review = excluded.review,
                         updated_at = CURRENT_TIMESTAMP""",
                    (g.user["id"], gid, rating, review or None),
                )
            if shelf:
                put_on_shelf(db, g.user["id"], gid, shelf)
            elif shelf_was:  # the page showed a shelf and the user cleared it
                put_on_shelf(db, g.user["id"], gid, "")
            db.commit()
            return redirect(url_for("game", game_id=gid))

    return render_template(
        "add.html", form=form, editing=editing, locked=locked, shelves=SHELVES
    )


@app.route("/game/<int:game_id>")
def game(game_id):
    db = get_db()
    surviving = enrich_game(db, game_id)
    if surviving != game_id:
        return redirect(url_for("game", game_id=surviving))
    row = db.execute(
        """SELECT g.id, g.title, g.cover_id, g.release_year, AVG(e.rating) AS avg, COUNT(e.id) AS n
           FROM games g LEFT JOIN entries e ON e.game_id = g.id
           WHERE g.id = ? GROUP BY g.id""",
        (game_id,),
    ).fetchone()
    if row is None:
        abort(404)
    entries = db.execute(
        f"""SELECT e.*, u.username, u.avatar_v, {REVIEW_STATS}
            FROM entries e
            JOIN users u ON u.id = e.user_id
            WHERE e.game_id = ? ORDER BY e.updated_at DESC, e.id DESC""",
        (g.user["id"] if g.user else -1, game_id),
    ).fetchall()
    mine = None
    if g.user:
        mine = db.execute(
            "SELECT id, review FROM entries WHERE user_id = ? AND game_id = ?",
            (g.user["id"], game_id),
        ).fetchone()
    my_shelf = get_shelf(db, g.user["id"], game_id) if g.user else ""
    return render_template(
        "game.html",
        game=row,
        entries=entries,
        mine=mine,
        my_shelf=my_shelf,
        shelves=SHELVES,
    )


@app.route("/game/<int:game_id>/shelf", methods=["POST"])
@login_required
def shelf_update(game_id):
    shelf = request.form.get("shelf", "")
    if shelf and shelf not in SHELVES:
        abort(400)
    db = get_db()
    if db.execute("SELECT 1 FROM games WHERE id = ?", (game_id,)).fetchone() is None:
        abort(404)
    put_on_shelf(db, g.user["id"], game_id, shelf)
    db.commit()
    flash(f"Added to {SHELVES[shelf]}." if shelf else "Removed from your shelves.")
    return redirect(url_for("game", game_id=game_id))


@app.route("/entry/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    db = get_db()
    db.execute(
        "DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, g.user["id"])
    )
    db.commit()
    return redirect(url_for("profile", username=g.user["username"]))
