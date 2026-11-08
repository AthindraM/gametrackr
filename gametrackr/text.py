"""Small text helpers."""

import re
import unicodedata


def norm_title(title):
    """Loose key so 'The Witcher® 3: Wild Hunt' matches 'The Witcher 3: Wild Hunt'."""
    t = re.sub(r"[™®©]", "", title or "")
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9]+", " ", t.replace("&", " and ")).strip()
    if t.startswith("the "):
        t = t[4:]
    return t or (title or "").strip().casefold()  # non-latin titles keep their own text
