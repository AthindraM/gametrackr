"""Steam account connection (OpenID) and library import."""

import json
import os
import re
import secrets
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import flash, g, redirect, request, session, url_for

from . import app
from .db import get_db
from .helpers import login_required
from .text import norm_title

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
    qs = urlencode(
        {
            "key": key,
            "steamid": steam_id,
            "format": "json",
            "include_appinfo": 1,
            "include_played_free_games": 1,
        }
    )
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?" + qs
    try:
        with urlopen(url, timeout=20) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise SteamError(
                "Steam rejected the server's API key. Check STEAM_API_KEY."
            )
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
    if (
        args.get("openid.mode") != "id_res"
        or args.get("openid.return_to") != return_to
        or not match
    ):
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
        db.execute(
            "UPDATE users SET steam_id = ? WHERE id = ?", (match.group(1), g.user["id"])
        )
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
    before = db.execute(
        "SELECT COUNT(*) FROM library WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    for title, minutes in playtime.values():
        db.execute(
            "INSERT OR IGNORE INTO games (title, norm_title) VALUES (?, ?)",
            (title, norm_title(title)),
        )
        gid = db.execute("SELECT id FROM games WHERE title = ?", (title,)).fetchone()[
            "id"
        ]
        db.execute(
            """INSERT INTO library (user_id, game_id, playtime_minutes) VALUES (?, ?, ?)
               ON CONFLICT (user_id, game_id) DO UPDATE SET
                 playtime_minutes = excluded.playtime_minutes""",
            (uid, gid, minutes),
        )
    db.commit()
    after = db.execute(
        "SELECT COUNT(*) FROM library WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    flash(f"Imported {after - before} new games ({len(playtime)} found on Steam).")
    return redirect(url_for("profile", username=g.user["username"]))
