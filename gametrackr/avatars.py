"""Profile pictures: upload, crop and resize, store in the database, and serve."""

import io
import time

from flask import Response, abort, flash, g, redirect, request, url_for
from markupsafe import Markup, escape
from PIL import Image, ImageOps

from . import app
from .db import get_db
from .helpers import login_required

AVATAR_SIZE = 256  # pixels, square
MAX_PIXELS = 25_000_000  # reject huge images before decoding them
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
BACKGROUND = (31, 38, 48)  # shown behind transparent images


class AvatarError(Exception):
    pass


def process_avatar(stream):
    """Validate an uploaded image and return it as a square JPEG.

    The picture is rebuilt from its pixels, so nothing from the original file
    (metadata, scripts, odd formats) is ever stored or served.
    """
    try:
        img = Image.open(stream)
        if img.format not in ALLOWED_FORMATS:
            raise AvatarError("Use a PNG, JPEG, WebP, or GIF image.")
        if img.width * img.height > MAX_PIXELS:
            raise AvatarError("That image is too large. Try one under 25 megapixels.")
        img.load()  # decodes the image (first frame of a GIF); fails on corrupt files
        img = ImageOps.exif_transpose(img)
        rgba = img.convert("RGBA")
        flat = Image.new("RGBA", rgba.size, BACKGROUND + (255,))
        flat.alpha_composite(rgba)
        square = ImageOps.fit(
            flat.convert("RGB"), (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS
        )
        out = io.BytesIO()
        square.save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue()
    except AvatarError:
        raise
    except Exception:  # untrusted file: any decoding problem means "not a usable image"
        raise AvatarError("Couldn't read that image. Try a different file.")


def avatar(person, size=40):
    """HTML for a user's picture, or a colored circle with their initial."""
    name = person["username"]
    try:
        version = person["avatar_v"]
    except (KeyError, IndexError):
        version = None
    size = int(size)
    if version:
        src = url_for("avatar_image", username=name, v=version)
        return Markup(
            f'<img class="avatar" src="{escape(src)}" alt="" '
            f'style="width:{size}px;height:{size}px">'
        )
    hue = sum(map(ord, name)) * 37 % 360
    return Markup(
        f'<span class="avatar" style="width:{size}px;height:{size}px;'
        f'font-size:{round(size * 0.45)}px;background:hsl({hue} 32% 36%)">'
        f"{escape(name[:1].upper())}</span>"
    )


app.add_template_global(avatar, "avatar")


@app.route("/avatar/<username>")
def avatar_image(username):
    row = (
        get_db()
        .execute(
            """SELECT a.data FROM avatars a JOIN users u ON u.id = a.user_id
               WHERE u.username = ?""",
            (username,),
        )
        .fetchone()
    )
    if row is None:
        abort(404)
    response = Response(row["data"], mimetype="image/jpeg")
    # The page URL carries a version number, so the picture can be cached for a year.
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/settings/avatar", methods=["POST"])
@login_required
def upload_avatar():
    back = redirect(url_for("settings", section="profile"))
    upload = request.files.get("avatar")
    if upload is None or not upload.filename:
        flash("Choose an image first.")
        return back
    try:
        data = process_avatar(upload.stream)
    except AvatarError as exc:
        flash(str(exc))
        return back
    db = get_db()
    db.execute(
        """INSERT INTO avatars (user_id, data) VALUES (?, ?)
           ON CONFLICT (user_id) DO UPDATE SET data = excluded.data""",
        (g.user["id"], data),
    )
    version = max(
        int(time.time()), (g.user["avatar_v"] or 0) + 1
    )  # new URL, fresh cache
    db.execute("UPDATE users SET avatar_v = ? WHERE id = ?", (version, g.user["id"]))
    db.commit()
    flash("Profile picture updated.")
    return back


@app.route("/settings/avatar/remove", methods=["POST"])
@login_required
def remove_avatar():
    db = get_db()
    db.execute("DELETE FROM avatars WHERE user_id = ?", (g.user["id"],))
    db.execute("UPDATE users SET avatar_v = NULL WHERE id = ?", (g.user["id"],))
    db.commit()
    flash("Profile picture removed.")
    return redirect(url_for("settings", section="profile"))
