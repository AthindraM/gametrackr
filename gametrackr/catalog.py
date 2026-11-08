"""Game catalog logic: finding games, linking them to IGDB, merging duplicates."""

import re

from flask import current_app, g

from .db import get_db
from .igdb import IgdbError, igdb_configured, igdb_search
from .text import norm_title


def merge_games(db, src, dst):
    """Fold a duplicate game row into another, keeping everyone's ratings and libraries."""
    db.execute("UPDATE OR IGNORE entries SET game_id = ? WHERE game_id = ?", (dst, src))
    db.execute("DELETE FROM entries WHERE game_id = ?", (src,))
    db.execute("UPDATE OR IGNORE library SET game_id = ? WHERE game_id = ?", (dst, src))
    db.execute("DELETE FROM library WHERE game_id = ?", (src,))
    db.execute(
        "UPDATE OR IGNORE shelf_items SET game_id = ? WHERE game_id = ?", (dst, src)
    )
    db.execute("DELETE FROM shelf_items WHERE game_id = ?", (src,))
    db.execute("DELETE FROM games WHERE id = ?", (src,))


def absorb_duplicates(db, keeper_id, norm):
    rows = db.execute(
        "SELECT id FROM games WHERE norm_title = ? AND igdb_id IS NULL AND id != ?",
        (norm, keeper_id),
    ).fetchall()
    for r in rows:
        merge_games(db, r["id"], keeper_id)


def ensure_game_from_igdb(db, ig):
    """Return the local game id for an IGDB game, linking or creating a row.

    Plain games (typed in or imported from Steam) whose title matches loosely are
    linked to the IGDB game and merged, so one game never appears twice.
    """
    norm = norm_title(ig["title"])
    row = db.execute(
        "SELECT id FROM games WHERE igdb_id = ?", (ig["igdb_id"],)
    ).fetchone()
    if row:
        absorb_duplicates(db, row["id"], norm)
        return row["id"]
    plain = db.execute(
        """SELECT id FROM games WHERE norm_title = ? AND igdb_id IS NULL
           ORDER BY (title = ? COLLATE NOCASE) DESC, id""",
        (norm, ig["title"]),
    ).fetchall()
    if plain:
        keeper = plain[0]["id"]
        for other in plain[1:]:
            merge_games(db, other["id"], keeper)
        db.execute(
            "UPDATE games SET igdb_id = ?, cover_id = ?, release_year = ?, norm_title = ? WHERE id = ?",
            (ig["igdb_id"], ig["cover_id"], ig["year"], norm, keeper),
        )
        clash = db.execute(
            "SELECT id FROM games WHERE title = ? AND id != ?", (ig["title"], keeper)
        ).fetchone()
        if clash is None:  # use IGDB's cleaner name, e.g. drop the (R) symbol
            db.execute("UPDATE games SET title = ? WHERE id = ?", (ig["title"], keeper))
        return keeper
    names = [ig["title"]]
    if ig["year"]:
        names.append(f"{ig['title']} ({ig['year']})")  # same title, different game
    names.append(f"{ig['title']} (IGDB {ig['igdb_id']})")
    for name in names:
        if (
            db.execute("SELECT 1 FROM games WHERE title = ?", (name,)).fetchone()
            is None
        ):
            cur = db.execute(
                """INSERT INTO games (title, igdb_id, cover_id, release_year, norm_title)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, ig["igdb_id"], ig["cover_id"], ig["year"], norm),
            )
            return cur.lastrowid
    raise IgdbError("Couldn't save that game.")


def enrich_game(db, game_id):
    """Try to link a plain (typed or Steam-imported) game to IGDB for its cover art.

    Returns the id of the surviving row, which differs if the game was merged.
    """
    row = db.execute(
        "SELECT id, title, igdb_id, igdb_checked FROM games WHERE id = ?", (game_id,)
    ).fetchone()
    if row is None or row["igdb_id"] or row["igdb_checked"] or not igdb_configured():
        return game_id
    want = norm_title(row["title"])
    try:
        query = re.sub(r"[™®©]", "", row["title"]).strip() or row["title"]
        for ig in igdb_search(query, 5):
            if norm_title(ig["title"]) == want:
                gid = ensure_game_from_igdb(db, ig)
                db.commit()
                return gid
    except IgdbError:
        return game_id  # try again on a later visit
    db.execute("UPDATE games SET igdb_checked = 1 WHERE id = ?", (game_id,))
    db.commit()
    return game_id


def search_results(q, local_limit, igdb_limit):
    """Games already on Gametrackr first, then matches from IGDB."""
    local = [
        {
            "id": r["id"],
            "igdb_id": r["igdb_id"],
            "title": r["title"],
            "year": r["release_year"],
            "cover_id": r["cover_id"],
            "avg": r["avg"] or 0,
            "n": r["n"],
        }
        for r in find_games(q, local_limit)
    ]
    if not (igdb_configured() and len(q) >= 3):
        return local
    try:
        remote = igdb_search(q, igdb_limit)
    except IgdbError as exc:
        current_app.logger.warning("IGDB search failed: %s", exc)
        return local
    known = {r["igdb_id"] for r in local if r["igdb_id"]}
    remote = [x for x in remote if x["igdb_id"] not in known]
    have = {}
    if remote:
        marks = ",".join("?" * len(remote))
        rows = (
            get_db()
            .execute(
                f"""SELECT g.id, g.igdb_id, AVG(e.rating) AS avg, COUNT(e.id) AS n
                FROM games g LEFT JOIN entries e ON e.game_id = g.id
                WHERE g.igdb_id IN ({marks}) GROUP BY g.id""",
                [x["igdb_id"] for x in remote],
            )
            .fetchall()
        )
        have = {r["igdb_id"]: r for r in rows}
    fresh = []
    for x in remote:
        mine = have.get(x["igdb_id"])
        fresh.append(
            {
                "id": mine["id"] if mine else None,
                "igdb_id": x["igdb_id"],
                "title": x["title"],
                "year": x["year"],
                "cover_id": x["cover_id"],
                "avg": (mine["avg"] or 0) if mine else 0,
                "n": mine["n"] if mine else 0,
            }
        )
    names = {norm_title(x["title"]) for x in fresh}
    local = [r for r in local if r["igdb_id"] or norm_title(r["title"]) not in names]
    return local + fresh


def find_games(q, limit):
    """Case-insensitive title search: prefix matches first, then most-rated."""
    uid = g.user["id"] if g.user else -1
    esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return (
        get_db()
        .execute(
            """SELECT g.id, g.title, g.igdb_id, g.cover_id, g.release_year,
                  AVG(e.rating) AS avg, COUNT(e.id) AS n
           FROM games g LEFT JOIN entries e ON e.game_id = g.id
           WHERE g.title LIKE ? ESCAPE '!'
             AND (g.igdb_id IS NOT NULL
                  OR g.id IN (SELECT game_id FROM entries)
                  OR g.id IN (SELECT game_id FROM library WHERE user_id = ?)
                  OR g.id IN (SELECT game_id FROM shelf_items WHERE user_id = ?))
           GROUP BY g.id
           ORDER BY (g.title LIKE ? ESCAPE '!') DESC, n DESC, g.title
           LIMIT ?""",
            (f"%{esc}%", uid, uid, f"{esc}%", limit),
        )
        .fetchall()
    )
