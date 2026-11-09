"""Settings pages: Profile (picture) and Connections (Steam)."""

import os

from flask import abort, g, redirect, render_template, url_for

from . import app
from .db import get_db
from .helpers import login_required

SETTINGS_SECTIONS = [("profile", "Profile"), ("connections", "Connections")]


@app.route("/settings")
@login_required
def settings_index():
    return redirect(url_for("settings", section=SETTINGS_SECTIONS[0][0]))


@app.route("/settings/<section>")
@login_required
def settings(section):
    if section not in dict(SETTINGS_SECTIONS):
        abort(404)
    count = (
        get_db()
        .execute("SELECT COUNT(*) FROM library WHERE user_id = ?", (g.user["id"],))
        .fetchone()[0]
    )
    return render_template(
        "settings.html",
        section=section,
        sections=SETTINGS_SECTIONS,
        count=count,
        configured=bool(os.environ.get("STEAM_API_KEY")),
    )
