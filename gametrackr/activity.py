"""What people have been up to: ratings and shelf changes.

Feeds the Following tab and the Recent Activity column on profiles, and groups
events into days ("Today", "Yesterday", "November 8, 2026") in the reader's time zone.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import request

UTC = timezone.utc
FEED_PAGE_SIZE = 50
PROFILE_ACTIVITY_COUNT = 10

# Which people's activity to look at (each appears twice in the query below).
FOLLOWED = "user_id IN (SELECT followee_id FROM follows WHERE follower_id = ?)"
ONE_USER = "user_id = ?"

# How an event reads: "alice is playing X" in the feed, "Playing X" on your own profile.
FEED_VERBS = {
    "rating": "rated",
    "playing": "is playing",
    "completed": "completed",
    "wishlist": "wishlisted",
}
OWN_VERBS = {
    "rating": "Rated",
    "playing": "Playing",
    "completed": "Completed",
    "wishlist": "Wishlisted",
}

_EVENTS = """
    SELECT 'rating' AS kind, id AS entry_id, user_id, game_id, rating, review, updated_at AS at, NULL AS shelf
      FROM entries WHERE {cond}
    UNION ALL
    SELECT 'shelf', NULL, user_id, game_id, NULL, NULL, added_at, shelf
      FROM shelf_items WHERE {cond}"""


def browser_timezone():
    """(the visitor's time zone, whether we really know it).

    app.js stores the browser's zone in a cookie; until it does, fall back to UTC.
    """
    name = request.cookies.get("tz", "")
    if name:
        try:
            return ZoneInfo(name), True
        except Exception:  # the cookie is untrusted input; any problem means "unknown"
            pass
    return UTC, False


def day_label(day, today, short=False):
    """Today, Yesterday, or a date: "November 8, 2026" (short: "Nov 8")."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    month = day.strftime("%b" if short else "%B")
    if short and day.year == today.year:
        return f"{month} {day.day}"
    return f"{month} {day.day}, {day.year}"


def clock(moment):
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def fetch(db, cond, params, limit, offset=0):
    """Newest-first events for the people matched by cond."""
    return db.execute(
        f"""SELECT a.*, u.username, u.avatar_v, g.title, g.cover_id
            FROM ({_EVENTS.format(cond=cond)}) a
            JOIN users u ON u.id = a.user_id
            JOIN games g ON g.id = a.game_id
            ORDER BY a.at DESC, a.game_id DESC, a.kind
            LIMIT ? OFFSET ?""",
        [*params, *params, limit, offset],
    ).fetchall()


def count(db, cond, params):
    return db.execute(
        f"""SELECT (SELECT COUNT(*) FROM entries WHERE {cond})
                 + (SELECT COUNT(*) FROM shelf_items WHERE {cond})""",
        [*params, *params],
    ).fetchone()[0]


def when(stamp, tz, now=None):
    """A stored timestamp as the reader sees it: "Today at 3:42 PM", "November 8, 2026 at 9:05 AM"."""
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    local = local.astimezone(tz)
    return f"{day_label(local.date(), today)} at {clock(local)}"


def decorate(rows, tz, now=None):
    """Add the local day, time, and wording to each event."""
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    events = []
    for row in rows:
        event = dict(row)
        local = datetime.strptime(row["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        local = local.astimezone(tz)
        key = event["shelf"] or "rating"
        event.update(
            day=local.date(),
            time=clock(local),
            verb=FEED_VERBS[key],
            own_verb=OWN_VERBS[key],
            day_label=day_label(local.date(), today),
            short_day=day_label(local.date(), today, short=True),
        )
        events.append(event)
    return events


def group_by_day(events):
    """Split newest-first events into [{"label": "Today", "events": [...]}, ...]."""
    sections = []
    for event in events:
        if not sections or sections[-1]["day"] != event["day"]:
            sections.append(
                {"day": event["day"], "label": event["day_label"], "events": []}
            )
        sections[-1]["events"].append(event)
    return sections
