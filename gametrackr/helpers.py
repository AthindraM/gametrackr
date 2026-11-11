"""Request hooks, login protection, and template filters."""

import os
import secrets

from flask import abort, flash, g, redirect, request, session, url_for

from . import app
from .db import get_db
from .igdb import cover_img


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = (
            get_db()
            .execute(
                "SELECT id, username, email, steam_id, avatar_v FROM users WHERE id = ?",
                (uid,),
            )
            .fetchone()
        )
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
    return {
        "csrf_token": session["csrf"],
        "user": g.get("user"),
        "cover_img": cover_img,
    }


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


def rating_tier(value):
    """UR gray, 1-4.9 red/orange, 5-7.9 yellow, 8-9.9 green, 10.0 blue."""
    if value is None:
        return "ur"
    v = float(f"{float(value):.1f}")
    if v < 5:
        return "low"
    if v < 8:
        return "mid"
    if v < 10:
        return "high"
    return "max"


@app.template_filter("tier")
def tier_filter(value):
    return rating_tier(value)


@app.template_filter("badge")
def badge_filter(value):
    return "UR" if value is None else f"{float(value):.1f}"


@app.template_global("asset")
def asset(path):
    """URL for a static file, versioned so browsers reload it after you edit it."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, path)))
    except OSError:
        version = 0
    return url_for("static", filename=path, v=version)


def find_person(db, username):
    """The user with this username (id, username, created_at, avatar_v), or a 404."""
    person = db.execute(
        "SELECT id, username, created_at, avatar_v FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if person is None:
        abort(404)
    return person


def safe_next(default):
    """Where to go after a button press: the page it came from, if it's on this site."""
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default
