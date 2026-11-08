"""IGDB client (search, covers, rate limiting) and cover-image helpers."""

import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from markupsafe import Markup

COVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


IGDB_TOKEN = {"value": None, "expires": 0.0}


IGDB_CACHE = {}


IGDB_CALLS = deque()


IGDB_STATE = {"down_until": 0.0}


# total_rating_count and follows measure popularity; the basic set is the fallback.
IGDB_FIELDS = (
    "fields name,first_release_date,cover.image_id,total_rating_count,follows;"
)
IGDB_FIELDS_BASIC = "fields name,first_release_date,cover.image_id;"


class IgdbError(Exception):
    pass


def igdb_mark_down(seconds=30):
    """After a network failure, skip IGDB briefly so pages don't hang on timeouts."""
    IGDB_STATE["down_until"] = time.time() + seconds


def igdb_configured():
    return bool(
        os.environ.get("IGDB_CLIENT_ID") and os.environ.get("IGDB_CLIENT_SECRET")
    )


def cover_url(cover_id, size="cover_small"):
    if cover_id and COVER_ID_RE.match(cover_id):
        return f"https://images.igdb.com/igdb/image/upload/t_{size}/{cover_id}.jpg"
    return None


def cover_img(cover_id, size="cover_small", cls="thumb", placeholder=True):
    url = cover_url(cover_id, size)
    if url:
        return Markup(
            f'<img class="{cls}" src="{url}" alt="" data-cover referrerpolicy="no-referrer" decoding="async">'
        )
    if placeholder and igdb_configured():
        return Markup(f'<span class="{cls} empty"></span>')
    return Markup("")


def igdb_token():
    now = time.time()
    if IGDB_TOKEN["value"] and now < IGDB_TOKEN["expires"] - 60:
        return IGDB_TOKEN["value"]
    body = urlencode(
        {
            "client_id": os.environ["IGDB_CLIENT_ID"],
            "client_secret": os.environ["IGDB_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        }
    ).encode()
    try:
        with urlopen(
            Request("https://id.twitch.tv/oauth2/token", data=body), timeout=6
        ) as resp:
            data = json.load(resp)
        IGDB_TOKEN["value"] = data["access_token"]
        IGDB_TOKEN["expires"] = now + float(data.get("expires_in", 3600))
    except (HTTPError, URLError, OSError, ValueError, KeyError):
        igdb_mark_down()
        raise IgdbError("Couldn't authenticate with IGDB.")
    return IGDB_TOKEN["value"]


def igdb_post(query):
    """Send an Apicalypse query to IGDB's /games endpoint (max 4 requests/second)."""
    now = time.time()
    if now < IGDB_STATE["down_until"]:
        raise IgdbError("IGDB is unavailable.")
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
            with urlopen(req, timeout=6) as resp:
                return json.load(resp)
        except HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                IGDB_TOKEN["value"] = (
                    None  # token expired or revoked: fetch a new one once
                )
                continue
            if exc.code >= 500:
                igdb_mark_down()
            raise IgdbError(f"IGDB error {exc.code}.")
        except (URLError, OSError, ValueError):
            igdb_mark_down()
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


def igdb_games(key, before, after):
    """Run a games query asking for popularity too; if IGDB rejects those fields
    (HTTP 400), retry with just the basics so search keeps working."""
    try:
        return igdb_cached(key, f"{before}{IGDB_FIELDS}{after}")
    except IgdbError as exc:
        if "400" not in str(exc):
            raise
        return igdb_cached(key + ("basic",), f"{before}{IGDB_FIELDS_BASIC}{after}")


def parse_igdb(item):
    name = " ".join(str(item.get("name") or "").split())[:120]
    if not name or not isinstance(item.get("id"), int):
        return None
    ts = item.get("first_release_date")
    year = (
        datetime.fromtimestamp(ts, timezone.utc).year
        if isinstance(ts, (int, float))
        else None
    )
    cover = (item.get("cover") or {}).get("image_id")
    popularity = sum(
        v
        for v in (item.get("total_rating_count"), item.get("follows"))
        if isinstance(v, int)
    )
    return {
        "igdb_id": item["id"],
        "title": name,
        "year": year,
        "cover_id": cover if cover and COVER_ID_RE.match(cover) else None,
        "popularity": popularity,
    }


def igdb_search(q, limit):
    safe = q.replace("\\", " ").replace('"', " ")
    items = igdb_games(
        ("s", safe.lower(), limit),
        f'search "{safe}"; ',
        f" where version_parent = null; limit {int(limit)};",
    )
    now = time.time()
    for item in items:  # picking a result later needs no second IGDB request
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            IGDB_CACHE[("g", item["id"])] = (now, [item])
    return [x for x in map(parse_igdb, items) if x]


def igdb_get(igdb_id):
    items = igdb_games(("g", igdb_id), "", f" where id = {int(igdb_id)};")
    parsed = [x for x in map(parse_igdb, items) if x]
    return parsed[0] if parsed else None
