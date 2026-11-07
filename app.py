"""Gametrackr - track and rate the games you play.

Run:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000
"""
import json
import os
import re
import secrets
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GAMETRACKR_DB") or os.path.join(BASE_DIR, "gametrackr.db")
KEY_PATH = os.path.join(BASE_DIR, ".secret_key")

app = Flask(__name__)


def load_secret_key():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(KEY_PATH, "w") as f:
        f.write(key)
    return key


app.secret_key = os.environ.get("SECRET_KEY") or load_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# In production the app sits behind a reverse proxy (Caddy/nginx). Set
# BEHIND_PROXY=1 so URLs (like the Steam sign-in return address) use the
# public https://gametrackr.dev address instead of the internal one.
if os.environ.get("BEHIND_PROXY"):
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    steam_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE COLLATE NOCASE,
    igdb_id INTEGER,
    cover_id TEXT,
    release_year INTEGER
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    rating REAL NOT NULL CHECK (rating >= 1.0 AND rating <= 10.0),
    review TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, game_id)
);
CREATE TABLE IF NOT EXISTS library (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    playtime_minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, game_id)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        cols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
        if "steam_id" not in cols:  # upgrade databases created before Steam support
            db.execute("ALTER TABLE users ADD COLUMN steam_id TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_steam ON users(steam_id)")
        gcols = [r[1] for r in db.execute("PRAGMA table_info(games)")]
        for col, ddl in (("igdb_id", "INTEGER"), ("cover_id", "TEXT"), ("release_year", "INTEGER")):
            if col not in gcols:  # upgrade databases created before IGDB support
                db.execute(f"ALTER TABLE games ADD COLUMN {col} {ddl}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_games_igdb ON games(igdb_id)")


# ---------------------------------------------------------------- helpers

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = get_db().execute(
            "SELECT id, username, email, steam_id FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if g.user is None:
            session.clear()
    if request.method == "POST":
        token = session.get("csrf")
        if not token or token != request.form.get("csrf"):
            abort(400, "Invalid form token. Reload the page and try again.")


@app.context_processor
def inject_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf_token": session["csrf"], "user": g.get("user"), "cover_img": cover_img}


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Log in to continue.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@app.template_filter("score")
def score(value):
    return f"{value:.1f}"


@app.template_filter("day")
def day(value):
    return (value or "")[:10]


# ---------------------------------------------------------------- routes

@app.route("/")
def home():
    if g.user is None:
        return render_template("landing.html")
    db = get_db()
    recent = db.execute(
        """SELECT e.*, u.username, g.title, g.cover_id FROM entries e
           JOIN users u ON u.id = e.user_id
           JOIN games g ON g.id = e.game_id
           ORDER BY e.updated_at DESC, e.id DESC LIMIT 20"""
    ).fetchall()
    top = db.execute(
        """SELECT g.id, g.title, g.cover_id, AVG(e.rating) AS avg, COUNT(*) AS n
           FROM games g JOIN entries e ON e.game_id = g.id
           GROUP BY g.id ORDER BY avg DESC, n DESC LIMIT 10"""
    ).fetchall()
    return render_template("home.html", recent=recent, top=top)


# ---------------------------------------------------------------- igdb

COVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
IGDB_TOKEN = {"value": None, "expires": 0.0}
IGDB_CACHE = {}
IGDB_CALLS = deque()
IGDB_FIELDS = "fields name,first_release_date,cover.image_id;"


class IgdbError(Exception):
    pass


def igdb_configured():
    return bool(os.environ.get("IGDB_CLIENT_ID") and os.environ.get("IGDB_CLIENT_SECRET"))


def cover_url(cover_id, size="cover_small"):
    if cover_id and COVER_ID_RE.match(cover_id):
        return f"https://images.igdb.com/igdb/image/upload/t_{size}/{cover_id}.jpg"
    return None


def cover_img(cover_id, size="cover_small", cls="thumb", placeholder=True):
    url = cover_url(cover_id, size)
    if url:
        return Markup(f'<img class="{cls}" src="{url}" alt="" loading="lazy">')
    if placeholder and igdb_configured():
        return Markup(f'<span class="{cls} empty"></span>')
    return Markup("")


def igdb_token():
    now = time.time()
    if IGDB_TOKEN["value"] and now < IGDB_TOKEN["expires"] - 60:
        return IGDB_TOKEN["value"]
    body = urlencode({
        "client_id": os.environ["IGDB_CLIENT_ID"],
        "client_secret": os.environ["IGDB_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    }).encode()
    try:
        with urlopen(Request("https://id.twitch.tv/oauth2/token", data=body), timeout=10) as resp:
            data = json.load(resp)
        IGDB_TOKEN["value"] = data["access_token"]
        IGDB_TOKEN["expires"] = now + float(data.get("expires_in", 3600))
    except (HTTPError, URLError, OSError, ValueError, KeyError):
        raise IgdbError("Couldn't authenticate with IGDB.")
    return IGDB_TOKEN["value"]


def igdb_post(query):
    """Send an Apicalypse query to IGDB's /games endpoint (max 4 requests/second)."""
    now = time.time()
    while IGDB_CALLS and now - IGDB_CALLS[0] > 1:
        IGDB_CALLS.popleft()
    if len(IGDB_CALLS) >= 4:
        raise IgdbError("IGDB is busy.")
    IGDB_CALLS.append(now)
    for attempt in (1, 2):
        req = Request(
            "https://api.igdb.com/v4/games",
            data=query.encode(),
            headers={
                "Client-ID": os.environ["IGDB_CLIENT_ID"],
                "Authorization": "Bearer " + igdb_token(),
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                IGDB_TOKEN["value"] = None  # token expired or revoked: fetch a new one once
                continue
            raise IgdbError(f"IGDB error {exc.code}.")
        except (URLError, OSError, ValueError):
            raise IgdbError("Couldn't reach IGDB.")


def igdb_cached(key, query):
    hit = IGDB_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    result = igdb_post(query)
    if len(IGDB_CACHE) > 500:
        IGDB_CACHE.clear()
    IGDB_CACHE[key] = (time.time(), result)
    return result


def parse_igdb(item):
    name = " ".join(str(item.get("name") or "").split())[:120]
    if not name or not isinstance(item.get("id"), int):
        return None
    ts = item.get("first_release_date")
    year = datetime.fromtimestamp(ts, timezone.utc).year if isinstance(ts, (int, float)) else None
    cover = (item.get("cover") or {}).get("image_id")
    return {
        "igdb_id": item["id"], "title": name, "year": year,
        "cover_id": cover if cover and COVER_ID_RE.match(cover) else None,
    }


def igdb_search(q, limit):
    safe = q.replace("\\", " ").replace('"', " ")
    query = f'search "{safe}"; {IGDB_FIELDS} where version_parent = null; limit {int(limit)};'
    items = igdb_cached(("s", safe.lower(), limit), query)
    now = time.time()
    for item in items:  # picking a result later needs no second IGDB request
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            IGDB_CACHE[("g", item["id"])] = (now, [item])
    return [x for x in map(parse_igdb, items) if x]


def igdb_get(igdb_id):
    items = igdb_cached(("g", igdb_id), f"{IGDB_FIELDS} where id = {int(igdb_id)};")
    parsed = [x for x in map(parse_igdb, items) if x]
    return parsed[0] if parsed else None


def ensure_game_from_igdb(db, ig):
    """Return the local game id for an IGDB game, creating or linking a row."""
    row = db.execute("SELECT id FROM games WHERE igdb_id = ?", (ig["igdb_id"],)).fetchone()
    if row:
        return row["id"]
    names = [ig["title"]]
    if ig["year"]:
        names.append(f"{ig['title']} ({ig['year']})")  # same title, different game
    names.append(f"{ig['title']} (IGDB {ig['igdb_id']})")
    for i, name in enumerate(names):
        existing = db.execute(
            "SELECT id, igdb_id FROM games WHERE title = ?", (name,)
        ).fetchone()
        if existing is None:
            cur = db.execute(
                "INSERT INTO games (title, igdb_id, cover_id, release_year) VALUES (?, ?, ?, ?)",
                (name, ig["igdb_id"], ig["cover_id"], ig["year"]),
            )
            return cur.lastrowid
        if existing["igdb_id"] is None and i == 0:  # a plain typed-in game: link it
            db.execute(
                "UPDATE games SET igdb_id = ?, cover_id = ?, release_year = ? WHERE id = ?",
                (ig["igdb_id"], ig["cover_id"], ig["year"], existing["id"]),
            )
            return existing["id"]
    raise IgdbError("Couldn't save that game.")


def search_results(q, local_limit, igdb_limit):
    """Games already on Gametrackr first, then matches from IGDB."""
    local = [
        {"id": r["id"], "igdb_id": r["igdb_id"], "title": r["title"],
         "year": r["release_year"], "cover_id": r["cover_id"],
         "avg": r["avg"] or 0, "n": r["n"]}
        for r in find_games(q, local_limit)
    ]
    if not (igdb_configured() and len(q) >= 3):
        return local
    try:
        remote = igdb_search(q, igdb_limit)
    except IgdbError as exc:
        app.logger.warning("IGDB search failed: %s", exc)
        return local
    known = {r["igdb_id"] for r in local if r["igdb_id"]}
    remote = [x for x in remote if x["igdb_id"] not in known]
    have = {}
    if remote:
        marks = ",".join("?" * len(remote))
        rows = get_db().execute(
            f"""SELECT g.id, g.igdb_id, AVG(e.rating) AS avg, COUNT(e.id) AS n
                FROM games g LEFT JOIN entries e ON e.game_id = g.id
                WHERE g.igdb_id IN ({marks}) GROUP BY g.id""",
            [x["igdb_id"] for x in remote],
        ).fetchall()
        have = {r["igdb_id"]: r for r in rows}
    fresh = []
    for x in remote:
        mine = have.get(x["igdb_id"])
        fresh.append({
            "id": mine["id"] if mine else None, "igdb_id": x["igdb_id"],
            "title": x["title"], "year": x["year"], "cover_id": x["cover_id"],
            "avg": (mine["avg"] or 0) if mine else 0, "n": mine["n"] if mine else 0,
        })
    names = {x["title"].lower() for x in fresh}
    local = [r for r in local if r["igdb_id"] or r["title"].lower() not in names]
    return local + fresh


def find_games(q, limit):
    """Case-insensitive title search: prefix matches first, then most-rated."""
    esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return get_db().execute(
        """SELECT g.id, g.title, g.igdb_id, g.cover_id, g.release_year,
                  AVG(e.rating) AS avg, COUNT(e.id) AS n
           FROM games g LEFT JOIN entries e ON e.game_id = g.id
           WHERE g.title LIKE ? ESCAPE '!'
           GROUP BY g.id
           ORDER BY (g.title LIKE ? ESCAPE '!') DESC, n DESC, g.title
           LIMIT ?""",
        (f"%{esc}%", f"{esc}%", limit),
    ).fetchall()


@app.route("/api/games/suggest")
def suggest_games():
    q = " ".join(request.args.get("q", "").split())[:100]
    if not q:
        return jsonify([])
    return jsonify([
        {"id": r["id"], "igdb_id": r["igdb_id"], "title": r["title"], "year": r["year"],
         "cover_url": cover_url(r["cover_id"]), "avg": r["avg"], "n": r["n"]}
        for r in search_results(q, 8, 8)[:8]
    ])


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


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("home"))
    form = {}
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        form = {"email": email, "username": username}
        error = None
        if not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif not USERNAME_RE.match(username):
            error = "Username must be 3-20 characters: letters, numbers, underscores."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        if error is None:
            try:
                db = get_db()
                cur = db.execute(
                    "INSERT INTO users (email, username, password_hash) VALUES (?, ?, ?)",
                    (email, username, generate_password_hash(password)),
                )
                db.commit()
                session.clear()
                session["user_id"] = cur.lastrowid
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                error = "That email or username is already taken."
        flash(error)
    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("home"))
    ident = ""
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute(
            "SELECT * FROM users WHERE username = ? OR email = ?", (ident, ident)
        ).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["id"]
            return redirect(url_for("home"))
        flash("Wrong username/email or password.")
    return render_template("login.html", ident=ident)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


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
        form = {"title": title, "rating": raw_rating, "review": review,
                "igdb_id": request.form.get("igdb_id", "")}
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
                db.execute("INSERT OR IGNORE INTO games (title) VALUES (?)", (title,))
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
           WHERE e.game_id = ? ORDER BY e.updated_at DESC""",
        (game_id,),
    ).fetchall()
    return render_template("game.html", game=row, entries=entries)


@app.route("/u/<username>")
def profile(username):
    db = get_db()
    person = db.execute(
        "SELECT id, username, created_at FROM users WHERE username = ?", (username,)
    ).fetchone()
    if person is None:
        abort(404)
    entries = db.execute(
        """SELECT e.*, g.title, g.cover_id FROM entries e JOIN games g ON g.id = e.game_id
           WHERE e.user_id = ? ORDER BY e.updated_at DESC""",
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
    return render_template("profile.html", person=person, entries=entries, avg=avg,
                           library=library)


@app.route("/entry/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_entry(entry_id):
    db = get_db()
    db.execute(
        "DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, g.user["id"])
    )
    db.commit()
    return redirect(url_for("profile", username=g.user["username"]))


# ---------------------------------------------------------------- steam

STEAM_OPENID = "https://steamcommunity.com/openid/login"
STEAM_CLAIMED_RE = re.compile(r"^https?://steamcommunity\.com/openid/id/(\d{17})$")


class SteamError(Exception):
    pass


def fetch_owned_games(steam_id):
    key = os.environ.get("STEAM_API_KEY")
    if not key:
        raise SteamError(
            "Steam import isn't set up on this server. Set the STEAM_API_KEY "
            "environment variable and restart the app."
        )
    qs = urlencode({
        "key": key, "steamid": steam_id, "format": "json",
        "include_appinfo": 1, "include_played_free_games": 1,
    })
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?" + qs
    try:
        with urlopen(url, timeout=20) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise SteamError("Steam rejected the server's API key. Check STEAM_API_KEY.")
        raise SteamError(f"Steam returned an error ({exc.code}). Try again later.")
    except (URLError, OSError, ValueError):
        raise SteamError("Couldn't reach Steam. Try again in a moment.")
    games = data.get("response", {}).get("games")
    if not games:
        raise SteamError(
            "No games found. In Steam, open Profile > Edit Profile > Privacy Settings "
            "and set Game details to Public, then try again."
        )
    return games


SETTINGS_SECTIONS = [("connections", "Connections")]


@app.route("/settings")
@login_required
def settings_index():
    return redirect(url_for("settings", section=SETTINGS_SECTIONS[0][0]))


@app.route("/settings/<section>")
@login_required
def settings(section):
    if section not in dict(SETTINGS_SECTIONS):
        abort(404)
    count = get_db().execute(
        "SELECT COUNT(*) FROM library WHERE user_id = ?", (g.user["id"],)
    ).fetchone()[0]
    return render_template(
        "settings.html", section=section, sections=SETTINGS_SECTIONS, count=count,
        configured=bool(os.environ.get("STEAM_API_KEY")),
    )


@app.route("/steam/connect")
@login_required
def steam_connect():
    session["steam_state"] = secrets.token_urlsafe(16)
    return_to = url_for("steam_callback", _external=True, state=session["steam_state"])
    ident = "http://specs.openid.net/auth/2.0/identifier_select"
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": request.host_url,
        "openid.identity": ident,
        "openid.claimed_id": ident,
    }
    return redirect(STEAM_OPENID + "?" + urlencode(params))


@app.route("/steam/callback")
@login_required
def steam_callback():
    expected = session.pop("steam_state", None)
    args = request.args
    if not expected or args.get("state") != expected:
        flash("Steam connection expired. Start again.")
        return redirect(url_for("settings", section="connections"))
    return_to = url_for("steam_callback", _external=True, state=expected)
    match = STEAM_CLAIMED_RE.match(args.get("openid.claimed_id", ""))
    if (args.get("openid.mode") != "id_res"
            or args.get("openid.return_to") != return_to or not match):
        flash("Steam sign-in was cancelled or failed.")
        return redirect(url_for("settings", section="connections"))

    # Ask Steam to confirm the response is genuine.
    check = {k: v for k, v in args.items() if k.startswith("openid.")}
    check["openid.mode"] = "check_authentication"
    try:
        req = Request(STEAM_OPENID, data=urlencode(check).encode())
        with urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
    except (URLError, OSError):
        flash("Couldn't reach Steam to confirm your sign-in. Try again.")
        return redirect(url_for("settings", section="connections"))
    if "is_valid:true" not in body:
        flash("Steam couldn't confirm your sign-in. Try again.")
        return redirect(url_for("settings", section="connections"))

    db = get_db()
    try:
        db.execute("UPDATE users SET steam_id = ? WHERE id = ?",
                   (match.group(1), g.user["id"]))
        db.commit()
    except sqlite3.IntegrityError:
        flash("That Steam account is already linked to another Gametrackr account.")
        return redirect(url_for("settings", section="connections"))
    flash("Steam account connected.")
    return redirect(url_for("settings", section="connections"))


@app.route("/steam/disconnect", methods=["POST"])
@login_required
def steam_disconnect():
    db = get_db()
    db.execute("UPDATE users SET steam_id = NULL WHERE id = ?", (g.user["id"],))
    db.commit()
    flash("Steam account disconnected. Games you already imported are kept.")
    return redirect(url_for("settings", section="connections"))


@app.route("/steam/import", methods=["POST"])
@login_required
def steam_import():
    if not g.user["steam_id"]:
        flash("Connect your Steam account first.")
        return redirect(url_for("settings", section="connections"))
    try:
        owned = fetch_owned_games(g.user["steam_id"])
    except SteamError as exc:
        flash(str(exc))
        return redirect(url_for("settings", section="connections"))

    playtime = {}
    for item in owned:
        title = " ".join((item.get("name") or "").split())[:120]
        if title:
            key = title.lower()
            minutes = int(item.get("playtime_forever") or 0)
            playtime[key] = (title, max(minutes, playtime.get(key, (title, 0))[1]))

    db = get_db()
    uid = g.user["id"]
    before = db.execute("SELECT COUNT(*) FROM library WHERE user_id = ?", (uid,)).fetchone()[0]
    for title, minutes in playtime.values():
        db.execute("INSERT OR IGNORE INTO games (title) VALUES (?)", (title,))
        gid = db.execute("SELECT id FROM games WHERE title = ?", (title,)).fetchone()["id"]
        db.execute(
            """INSERT INTO library (user_id, game_id, playtime_minutes) VALUES (?, ?, ?)
               ON CONFLICT (user_id, game_id) DO UPDATE SET
                 playtime_minutes = excluded.playtime_minutes""",
            (uid, gid, minutes),
        )
    db.commit()
    after = db.execute("SELECT COUNT(*) FROM library WHERE user_id = ?", (uid,)).fetchone()[0]
    flash(f"Imported {after - before} new games ({len(playtime)} found on Steam).")
    return redirect(url_for("profile", username=g.user["username"]))


@app.errorhandler(400)
@app.errorhandler(404)
def error_page(err):
    return render_template("error.html", err=err), err.code


# ---------------------------------------------------------------- templates

CSS = """
:root{--bg:#161b22;--surface:#1f2630;--line:#2d3644;--text:#fff;--muted:#9aa7b8;--accent:#5d8ec9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--text);text-decoration-color:var(--accent);text-underline-offset:3px}
a:hover{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
header{border-bottom:1px solid var(--line)}
.bar,main{max-width:760px;margin:0 auto;padding:0 20px}
.bar{display:flex;align-items:center;gap:20px;height:56px}
.logo{font-weight:700;font-size:1.2rem;text-decoration:none;margin-right:4px;letter-spacing:-.01em}
.logo span{color:var(--accent)}
main{padding-top:32px;padding-bottom:64px}
h1{font-size:1.6rem;margin:0 0 20px}
h2{font-size:1.05rem;margin:36px 0 12px;color:var(--muted);font-weight:600}
.muted{color:var(--muted)}
.flash{background:var(--surface);border-left:3px solid var(--accent);padding:10px 14px;margin-bottom:20px}
form.stack{display:grid;gap:16px;max-width:420px}
label{display:grid;gap:6px;font-size:.9rem;color:var(--muted)}
input,textarea{background:var(--surface);border:1px solid var(--line);border-radius:4px;color:var(--text);font:inherit;padding:9px 11px;width:100%}
input:focus,textarea:focus{border-color:var(--accent);outline:none}
textarea{min-height:130px;resize:vertical}
button,.btn{background:var(--accent);border:0;border-radius:4px;color:#fff;cursor:pointer;font:inherit;font-weight:600;padding:9px 16px;text-decoration:none;display:inline-block}
button:hover,.btn:hover{background:#7aa3d6;color:#fff}
button.link{background:none;color:var(--muted);font-weight:400;padding:0}
button.link:hover{background:none;color:var(--text)}
.row{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--line)}
.row .main{min-width:0;flex:1}
.row .grow{flex:1;min-width:0}
.score{font-weight:700;font-variant-numeric:tabular-nums;color:var(--accent);white-space:nowrap}
.review{margin:6px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}
.meta{font-size:.85rem;color:var(--muted)}
.big{font-size:2.4rem;font-weight:700;color:var(--accent);line-height:1}
form.inline{display:inline;margin:0}
.actions{display:flex;gap:12px;font-size:.85rem}
.menu{position:relative}
.menu summary{list-style:none;cursor:pointer;padding:6px 0;user-select:none}
.menu summary::-webkit-details-marker{display:none}
.menu summary::after{content:"\\25BE";margin-left:6px;color:var(--muted);font-size:.8em}
.menu[open] summary{color:var(--accent)}
.menu .pop{position:absolute;right:0;top:100%;margin-top:6px;min-width:170px;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:6px 0;z-index:10}
.menu .pop form{display:block;margin:0}
.menu .pop a,.menu .pop button{display:block;width:100%;text-align:left;padding:8px 14px;background:none;border:0;border-radius:0;color:var(--text);font:inherit;font-weight:400;text-decoration:none;cursor:pointer}
.menu .pop a:hover,.menu .pop button:hover{background:var(--line);color:var(--text)}
.menu .pop hr{border:0;border-top:1px solid var(--line);margin:6px 0}
.menu .pop button.danger,.menu .pop button.danger:hover{color:#ff6b6b}
.landing{display:grid;grid-template-columns:1fr 300px;gap:56px;align-items:center;min-height:60vh}
.landing h1{font-size:clamp(1.9rem,4.8vw,2.7rem);line-height:1.25;letter-spacing:-.01em;margin:0}
.landing h1 span{display:block}
.landing h1 .blue{font-weight:inherit;color:var(--accent)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:24px;display:grid;gap:12px}
.panel .btn{display:block;width:100%;text-align:center;padding:12px 16px}
.btn.light{background:#fff;color:var(--bg)}
.btn.light:hover{background:#dfe6ef;color:var(--bg)}
@media (max-width:700px){.landing{grid-template-columns:1fr;gap:32px;min-height:0;padding-top:16px}}
.thumb{width:36px;height:50px;object-fit:cover;border-radius:3px;background:var(--surface);flex:none;display:block}
.thumb.empty{border:1px solid var(--line)}
.gamehead{display:flex;gap:24px;align-items:flex-start}
.cover-lg{width:150px;aspect-ratio:264/374;object-fit:cover;border-radius:4px;flex:none;background:var(--surface)}
@media (max-width:480px){.cover-lg{width:110px}}
.ac-list .lead{display:flex;gap:10px;align-items:center;min-width:0}
.ac-list .thumb{width:28px;height:40px}
.navsearch{margin-right:auto;flex:0 1 260px}
.navsearch input{padding:6px 10px}
.ac{position:relative;display:block}
.ac-list{position:absolute;left:0;right:0;top:100%;margin:4px 0 0;padding:4px 0;list-style:none;background:var(--surface);border:1px solid var(--line);border-radius:6px;z-index:20;max-height:320px;overflow-y:auto}
.ac-list li{display:flex;justify-content:space-between;gap:12px;padding:8px 12px;cursor:pointer;color:var(--text)}
.ac-list li.active,.ac-list li:hover{background:var(--line)}
.ac-list .meta{white-space:nowrap}
@media (max-width:600px){.bar{height:auto;flex-wrap:wrap;padding:10px 20px}.navsearch{order:5;flex:1 1 100%;margin:0}}
.settings{display:grid;grid-template-columns:170px 1fr;gap:40px}
.tabs{display:flex;flex-direction:column;gap:2px}
.tab{padding:8px 12px;border-left:2px solid transparent;color:var(--muted);text-decoration:none}
.tab:hover{color:var(--text)}
.tab.active{border-left-color:var(--accent);color:var(--text);background:var(--surface)}
@media (max-width:600px){.settings{grid-template-columns:1fr;gap:20px}.tabs{flex-direction:row}}
"""

BASE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Gametrackr{% endblock %}</title>
<style>""" + CSS + """</style></head><body>
<header><div class="bar">
<a class="logo" href="{{ url_for('home') }}">Game<span>trackr</span></a>
<form class="navsearch" action="{{ url_for('search') }}" method="get" role="search">
<span class="ac"><input type="search" name="q" placeholder="Search games" aria-label="Search games" value="{{ request.args.get('q', '') if request.endpoint == 'search' else '' }}" data-suggest="go" autocomplete="off" required></span></form>
{% if user %}
<a href="{{ url_for('add') }}">Add game</a>
<details class="menu"><summary>{{ user.username }}</summary>
<div class="pop">
<a href="{{ url_for('profile', username=user.username) }}">Profile</a>
<a href="{{ url_for('settings_index') }}">Settings</a>
<hr>
<form method="post" action="{{ url_for('logout') }}">
<input type="hidden" name="csrf" value="{{ csrf_token }}"><button class="danger">Log out</button></form>
</div></details>
{% else %}
<a href="{{ url_for('login') }}">Log in</a>
<a class="btn" href="{{ url_for('register') }}">Sign up</a>
{% endif %}
</div></header>
<main>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{% block body %}{% endblock %}
</main>
<script>
document.addEventListener("click",e=>document.querySelectorAll("details.menu[open]").forEach(d=>{if(!d.contains(e.target))d.open=false}));
document.addEventListener("keydown",e=>{if(e.key==="Escape")document.querySelectorAll("details.menu[open]").forEach(d=>d.open=false)});
</script>
<script>
document.querySelectorAll("input[data-suggest]").forEach(function(input){
  var mode=input.dataset.suggest, box=input.parentElement;
  var hidden=input.dataset.igdbField?document.getElementById(input.dataset.igdbField):null;
  var list=document.createElement("ul"); list.className="ac-list"; list.hidden=true; box.appendChild(list);
  var items=[], active=-1, timer=null, seq=0;
  function close(){list.hidden=true; active=-1;}
  function choose(game){
    if(mode==="go"){location.href=game.id?"/game/"+game.id:"/game/igdb/"+game.igdb_id;}
    else{input.value=game.title; if(hidden)hidden.value=game.igdb_id||""; close();}
  }
  function render(){
    list.textContent="";
    items.forEach(function(game,i){
      var li=document.createElement("li"); if(i===active)li.className="active";
      var lead=document.createElement("span"); lead.className="lead";
      if(game.cover_url){var img=document.createElement("img"); img.className="thumb"; img.alt=""; img.src=game.cover_url; lead.appendChild(img);}
      var t=document.createElement("span"); t.textContent=game.title+(game.year?" ("+game.year+")":""); lead.appendChild(t);
      li.appendChild(lead);
      if(game.n){var m=document.createElement("span"); m.className="meta"; m.textContent=game.avg.toFixed(1); li.appendChild(m);}
      li.addEventListener("mousedown",function(e){e.preventDefault(); choose(game);});
      list.appendChild(li);
    });
    list.hidden=!items.length;
  }
  input.addEventListener("input",function(){
    if(hidden)hidden.value="";
    clearTimeout(timer);
    var q=input.value.trim();
    if(!q){items=[]; close(); return;}
    timer=setTimeout(function(){
      var mine=++seq;
      fetch("/api/games/suggest?q="+encodeURIComponent(q)).then(function(r){return r.ok?r.json():[];}).then(function(data){
        if(mine!==seq)return; items=data; active=-1; render();
      }).catch(function(){});
    },250);
  });
  input.addEventListener("keydown",function(e){
    if(list.hidden||!items.length)return;
    if(e.key==="ArrowDown"){e.preventDefault(); active=(active+1)%items.length; render();}
    else if(e.key==="ArrowUp"){e.preventDefault(); active=(active-1+items.length)%items.length; render();}
    else if(e.key==="Enter"&&active>=0){e.preventDefault(); choose(items[active]);}
    else if(e.key==="Escape"){close();}
  });
  input.addEventListener("blur",close);
});
</script></body></html>"""

HOME = """{% extends "base.html" %}{% block body %}
<h2 style="margin-top:0">Recent activity</h2>
{% for e in recent %}<div class="row">{{ cover_img(e.cover_id) }}<div class="main">
<a href="{{ url_for('profile', username=e.username) }}">{{ e.username }}</a> rated
<a href="{{ url_for('game', game_id=e.game_id) }}">{{ e.title }}</a>
{% if e.review %}<p class="review muted">{{ e.review[:240] }}{% if e.review|length > 240 %}&hellip;{% endif %}</p>{% endif %}
</div><span class="score">{{ e.rating|score }}</span></div>
{% else %}<p class="muted">Nothing here yet. <a href="{{ url_for('add') }}">Add a game.</a></p>{% endfor %}
<h2>Top rated games</h2>
{% for t in top %}<div class="row">{{ cover_img(t.cover_id) }}<a class="grow" href="{{ url_for('game', game_id=t.id) }}">{{ t.title }}</a>
<span class="score">{{ t.avg|score }} <span class="meta">({{ t.n }})</span></span></div>
{% else %}<p class="muted">No games rated yet.</p>{% endfor %}
{% endblock %}"""

LANDING = """{% extends "base.html" %}{% block body %}
<div class="landing">
<h1><span><b class="blue">Track</b> games you&rsquo;ve played,</span>
<span><b class="blue">Find</b> new games,</span>
<span><b class="blue">Share</b> games with your friends!</span></h1>
<div class="panel">
<a class="btn" href="{{ url_for('register') }}">Sign Up</a>
<a class="btn light" href="{{ url_for('login') }}">Log In</a>
</div></div>
{% endblock %}"""

REGISTER = """{% extends "base.html" %}{% block title %}Sign up - Gametrackr{% endblock %}{% block body %}
<h1>Create your account</h1>
<form class="stack" method="post"><input type="hidden" name="csrf" value="{{ csrf_token }}">
<label>Email<input type="email" name="email" value="{{ form.email }}" required autofocus></label>
<label>Username<input name="username" value="{{ form.username }}" required minlength="3" maxlength="20" pattern="[A-Za-z0-9_]+"></label>
<label>Password<input type="password" name="password" required minlength="8"></label>
<button>Create account</button></form>
<p class="muted">Already have an account? <a href="{{ url_for('login') }}">Log in</a></p>
{% endblock %}"""

LOGIN = """{% extends "base.html" %}{% block title %}Log in - Gametrackr{% endblock %}{% block body %}
<h1>Log in</h1>
<form class="stack" method="post"><input type="hidden" name="csrf" value="{{ csrf_token }}">
<label>Username or email<input name="identifier" value="{{ ident }}" required autofocus></label>
<label>Password<input type="password" name="password" required></label>
<button>Log in</button></form>
<p class="muted">New here? <a href="{{ url_for('register') }}">Create an account</a></p>
{% endblock %}"""

ADD = """{% extends "base.html" %}{% block title %}Add game - Gametrackr{% endblock %}{% block body %}
<h1>{{ "Edit your rating" if editing else "Add a game" }}</h1>
<form class="stack" method="post"><input type="hidden" name="csrf" value="{{ csrf_token }}">
<input type="hidden" id="igdb_id" name="igdb_id" value="{{ form.igdb_id }}">
<label>Game title<span class="ac"><input name="title" value="{{ form.title }}" required maxlength="120" data-suggest="fill" data-igdb-field="igdb_id" autocomplete="off" {{ "readonly" if locked }} {{ "" if locked else "autofocus" }}></span></label>
<label>Rating (1.0 - 10.0)<input type="number" name="rating" value="{{ form.rating }}" min="1" max="10" step="0.1" required></label>
<label>Review (optional)<textarea name="review" maxlength="5000">{{ form.review }}</textarea></label>
<button>Save</button></form>
{% endblock %}"""

GAME = """{% extends "base.html" %}{% block title %}{{ game.title }} - Gametrackr{% endblock %}{% block body %}
<div class="gamehead">{{ cover_img(game.cover_id, "cover_big", "cover-lg", False) }}<div>
<h1>{{ game.title }}</h1>
{% if game.release_year %}<p class="meta" style="margin-top:-12px">{{ game.release_year }}</p>{% endif %}
{% if game.n %}<p><span class="big">{{ game.avg|score }}</span>
<span class="meta">&nbsp;average from {{ game.n }} rating{{ "" if game.n == 1 else "s" }}</span></p>{% endif %}
<p><a class="btn" href="{{ url_for('add', game=game.id) }}">{{ "Rate this game" if user else "Log in to rate" }}</a></p>
</div></div>
<h2>Reviews</h2>
{% for e in entries %}<div class="row"><div class="main">
<a href="{{ url_for('profile', username=e.username) }}">{{ e.username }}</a>
<span class="meta">{{ e.updated_at|day }}</span>
{% if e.review %}<p class="review">{{ e.review }}</p>{% endif %}</div>
<span class="score">{{ e.rating|score }}</span></div>
{% else %}<p class="muted">No ratings yet.</p>{% endfor %}
{% endblock %}"""

PROFILE = """{% extends "base.html" %}{% block title %}{{ person.username }} - Gametrackr{% endblock %}{% block body %}
<h1>{{ person.username }}</h1>
<p class="muted">{{ entries|length }} game{{ "" if entries|length == 1 else "s" }} rated
{% if avg %}, average {{ avg|score }}{% endif %}. Member since {{ person.created_at|day }}.</p>
{% set mine = user and user.id == person.id %}
{% for e in entries %}<div class="row">{{ cover_img(e.cover_id) }}<div class="main">
<a href="{{ url_for('game', game_id=e.game_id) }}">{{ e.title }}</a>
{% if e.review %}<p class="review muted">{{ e.review }}</p>{% endif %}
{% if mine %}<div class="actions"><a href="{{ url_for('add', game=e.game_id) }}">Edit</a>
<form class="inline" method="post" action="{{ url_for('delete_entry', entry_id=e.id) }}"
onsubmit="return confirm('Remove this game from your list?')">
<input type="hidden" name="csrf" value="{{ csrf_token }}"><button class="link">Remove</button></form></div>{% endif %}
</div><span class="score">{{ e.rating|score }}</span></div>
{% else %}<p class="muted">No games yet.{% if mine %} <a href="{{ url_for('add') }}">Add your first game.</a>{% endif %}</p>{% endfor %}
{% if library %}<h2>Unrated in your library</h2>
{% for l in library %}<div class="row">{{ cover_img(l.cover_id) }}<div class="main">
<a href="{{ url_for('game', game_id=l.game_id) }}">{{ l.title }}</a>
<span class="meta">{{ (l.playtime_minutes / 60)|round(1) }} h played</span></div>
<a href="{{ url_for('add', game=l.game_id) }}">Rate</a></div>{% endfor %}{% endif %}
{% endblock %}"""

SETTINGS = """{% extends "base.html" %}{% block title %}Settings - Gametrackr{% endblock %}{% block body %}
<h1>Settings</h1>
<div class="settings">
<nav class="tabs">{% for key, label in sections %}
<a class="tab {{ 'active' if key == section }}" href="{{ url_for('settings', section=key) }}">{{ label }}</a>{% endfor %}</nav>
<div>
{% if section == "connections" %}
<h2 style="margin-top:0">Steam</h2>
{% if user.steam_id %}
<p class="muted">Connected to Steam ID {{ user.steam_id }}.</p>
{% if configured %}
<p>Import your Steam library to add every game you own to your list, ready to rate.
Your Steam profile's Game details must be set to Public.</p>
<form class="inline" method="post" action="{{ url_for('steam_import') }}">
<input type="hidden" name="csrf" value="{{ csrf_token }}">
<button>{{ "Import again" if count else "Import my games" }}</button></form>
{% if count %}<p class="meta">{{ count }} games in your library.</p>{% endif %}
{% else %}<p class="muted">Importing isn't set up on this server yet. Set the STEAM_API_KEY environment variable and restart.</p>{% endif %}
<p style="margin-top:28px"><form class="inline" method="post" action="{{ url_for('steam_disconnect') }}">
<input type="hidden" name="csrf" value="{{ csrf_token }}"><button class="link">Disconnect Steam account</button></form></p>
{% else %}
<p class="muted">Connect your Steam account to import the games you own. You'll sign in on Steam's own site; Gametrackr never sees your Steam password.</p>
<p><a class="btn" href="{{ url_for('steam_connect') }}">Connect Steam</a></p>
{% endif %}
{% endif %}
</div></div>
{% endblock %}"""

ERROR = """{% extends "base.html" %}{% block body %}
<h1>{{ err.code }}</h1><p class="muted">{{ err.description }}</p>
<p><a href="{{ url_for('home') }}">Back to home</a></p>{% endblock %}"""

SEARCH = """{% extends "base.html" %}{% block title %}{{ q or "Search" }} - Gametrackr{% endblock %}{% block body %}
<h1>{% if q %}Results for &ldquo;{{ q }}&rdquo;{% else %}Search games{% endif %}</h1>
{% for r in results %}<div class="row">{{ cover_img(r.cover_id) }}
<a class="grow" href="{{ url_for('game', game_id=r.id) if r.id else url_for('game_from_igdb', igdb_id=r.igdb_id) }}">{{ r.title }}{% if r.year %} <span class="meta">({{ r.year }})</span>{% endif %}</a>
{% if r.n %}<span class="score">{{ r.avg|score }} <span class="meta">({{ r.n }})</span></span>{% else %}<span class="meta">Not rated yet</span>{% endif %}</div>
{% else %}{% if q %}<p class="muted">No games match that search.</p>
<p><a class="btn" href="{{ url_for('add', title=q) }}">Add &ldquo;{{ q }}&rdquo;</a></p>
{% else %}<p class="muted">Type a game title in the search box.</p>{% endif %}{% endfor %}
{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "home.html": HOME, "landing.html": LANDING, "register.html": REGISTER,
    "login.html": LOGIN, "add.html": ADD, "game.html": GAME,
    "profile.html": PROFILE, "error.html": ERROR, "settings.html": SETTINGS, "search.html": SEARCH,
})

init_db()

if __name__ == "__main__":
    app.run(debug=True)
