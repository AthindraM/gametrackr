"""Favicon routes and the error page."""

from flask import render_template, send_from_directory

from . import app
from .config import FAVICON_DIR

ICON_FILES = (
    "favicon.ico",
    "favicon-16x16.png",
    "favicon-32x32.png",
    "apple-touch-icon.png",
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
    "site.webmanifest",
)


def make_icon_view(filename):
    mimetype = (
        "application/manifest+json" if filename.endswith(".webmanifest") else None
    )

    def view():
        return send_from_directory(
            FAVICON_DIR, filename, mimetype=mimetype, max_age=86400
        )

    return view


for _i, _name in enumerate(ICON_FILES):
    app.add_url_rule(
        "/" + _name, endpoint=f"site_icon_{_i}", view_func=make_icon_view(_name)
    )


@app.errorhandler(400)
@app.errorhandler(404)
def error_page(err):
    return render_template("error.html", err=err), err.code
