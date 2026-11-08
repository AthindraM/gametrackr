"""Paths and settings shared across the app."""

import os
import secrets

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root


DB_PATH = os.environ.get("GAMETRACKR_DB") or os.path.join(BASE_DIR, "gametrackr.db")


KEY_PATH = os.path.join(BASE_DIR, ".secret_key")


FAVICON_DIR = os.path.join(BASE_DIR, "static", "favicons")


def load_secret_key():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(KEY_PATH, "w") as f:
        f.write(key)
    return key
