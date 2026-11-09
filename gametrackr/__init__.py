"""Gametrackr - track and rate the games you play."""

import os

from flask import Flask

from .config import BASE_DIR, load_secret_key

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = os.environ.get("SECRET_KEY") or load_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # largest upload: a profile picture

# In production the app sits behind a reverse proxy (Caddy/nginx/Render). Set
# BEHIND_PROXY=1 so URLs (like the Steam sign-in return address) use the
# public https://gametrackr.dev address instead of the internal one.
if os.environ.get("BEHIND_PROXY"):
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True

from .db import close_db, init_db  # noqa: E402
from .igdb import cover_url  # noqa: E402

app.teardown_appcontext(close_db)
app.add_template_global(cover_url, "cover_url")

# Importing these modules registers their routes and template helpers.
from . import (
    auth,
    avatars,
    follows,
    games,
    helpers,
    misc,
    profiles,
    settings,
    steam,
)  # noqa: E402,F401

init_db()
