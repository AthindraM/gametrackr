"""Home page, search, game pages, profiles, and adding/removing ratings."""

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

from . import app
from .catalog import enrich_game, ensure_game_from_igdb, search_results
from .db import get_db
from .helpers import login_required
from .igdb import IgdbError, cover_url, igdb_configured, igdb_get
from .text import norm_title


@app.route("/")
def home():
    if g.user is None:
        return render_template("landing.html")
    db = get_db()
    recent = db.execute("""SELECT e.*, u.username, g.title, g.cover_id FROM entries e
           JOIN users u ON u.id = e.user_id
           JOIN games g ON g.id = e.game_id
           ORDER BY e.updated_at DESC, e.id DESC LIMIT 20""").fetchall()
    top = db.execute(
        """SELECT g.id, g.title, g.cover_id, AVG(e.rating) AS avg, COUNT(*) AS n
           FROM games g JOIN entries e ON e.game_id = g.id
           GROUP BY g.id ORDER BY avg DESC, n DESC LIMIT 12"""
    ).fetchall()
    return render_template("home.html", recent=recent, top=top)


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
            for r in search_results(q, 8, 8)[:8]
        ]
    )


@app.route("/search")
def search():
    q = " ".join(request.args.get("q", "").split())[:100]
    results = search_results(q, 50, 20) if q else []
    return render_template("search.html", q=q, results=results)


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
    form = {"title": "", "rating": "", "review": ""}
    editing = False
    locked = False

    game_id = request.args.get("game", type=int)
    if request.method == "GET" and game_id:
        row = db.execute(
            """SELECT g.title, e.rating, e.review FROM games g
               LEFT JOIN entries e ON e.game_id = g.id AND e.user_id = ?
               WHERE g.id = ?""",
            (g.user["id"], game_id),
        ).fetchone()
        if row:
            form["title"] = row["title"]
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
        form = {
            "title": title,
            "rating": raw_rating,
            "review": review,
            "igdb_id": request.form.get("igdb_id", ""),
        }
        error = None
        rating = None
        try:
            rating = round(float(raw_rating), 1)
            if not 1.0 <= rating <= 10.0:
                raise ValueError
        except ValueError:
            error = "Rating must be a number from 1.0 to 10.0."
        if not title or len(title) > 120:
            error = "Enter a game title (up to 120 characters)."
        elif len(review) > 5000:
            error = "Review is too long (5000 characters max)."

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
            db.execute(
                """INSERT INTO entries (user_id, game_id, rating, review)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (user_id, game_id) DO UPDATE SET
                     rating = excluded.rating,
                     review = excluded.review,
                     updated_at = CURRENT_TIMESTAMP""",
                (g.user["id"], gid, rating, review or None),
            )
            db.commit()
            return redirect(url_for("game", game_id=gid))

    return render_template("add.html", form=form, editing=editing, locked=locked)


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
        """SELECT e.*, u.username FROM entries e
           JOIN users u ON u.id = e.user_id
           WHERE e.game_id = ? ORDER BY e.updated_at DESC, e.id DESC""",
        (game_id,),
    ).fetchall()
    mine = None
    if g.user:
        mine = db.execute(
            "SELECT id FROM entries WHERE user_id = ? AND game_id = ?",
            (g.user["id"], game_id),
        ).fetchone()
    return render_template("game.html", game=row, entries=entries, mine=mine)


@app.route("/u/<username>")
def profile(username):
    db = get_db()
    person = db.execute(
        "SELECT id, username, created_at FROM users WHERE username = ?", (username,)
    ).fetchone()
    if person is None:
        abort(404)
    if igdb_configured() and g.user and g.user["id"] == person["id"]:
        # Fetch cover art for a few unmatched games per visit (most-played first).
        pending = db.execute(
            """SELECT g.id FROM games g
               WHERE g.igdb_id IS NULL AND g.igdb_checked = 0
                 AND (g.id IN (SELECT game_id FROM library WHERE user_id = ?)
                      OR g.id IN (SELECT game_id FROM entries WHERE user_id = ?))
               ORDER BY COALESCE((SELECT playtime_minutes FROM library l
                                  WHERE l.game_id = g.id AND l.user_id = ?), 0) DESC
               LIMIT 3""",
            (person["id"],) * 3,
        ).fetchall()
        for r in pending:
            enrich_game(db, r["id"])
    entries = db.execute(
        """SELECT e.*, g.title, g.cover_id FROM entries e JOIN games g ON g.id = e.game_id
           WHERE e.user_id = ? ORDER BY e.updated_at DESC, e.id DESC""",
        (person["id"],),
    ).fetchall()
    avg = sum(e["rating"] for e in entries) / len(entries) if entries else None
    library = []
    if g.user and g.user["id"] == person["id"]:
        library = db.execute(
            """SELECT l.game_id, l.playtime_minutes, g.title, g.cover_id FROM library l
               JOIN games g ON g.id = l.game_id
               WHERE l.user_id = ?
                 AND l.game_id NOT IN (SELECT game_id FROM entries WHERE user_id = ?)
               ORDER BY l.playtime_minutes DESC, g.title""",
            (person["id"], person["id"]),
        ).fetchall()
    return render_template(
        "profile.html", person=person, entries=entries, avg=avg, library=library
    )


@app.route("/entry/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    db = get_db()
    db.execute(
        "DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, g.user["id"])
    )
    db.commit()
    return redirect(url_for("profile", username=g.user["username"]))
