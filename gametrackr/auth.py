"""Sign up, log in, log out."""

import re
import sqlite3

from flask import flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import app
from .db import get_db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


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
        row = (
            get_db()
            .execute(
                "SELECT * FROM users WHERE username = ? OR email = ?", (ident, ident)
            )
            .fetchone()
        )
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
