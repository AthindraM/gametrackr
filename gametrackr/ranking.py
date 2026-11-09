"""Search ranking: how well the title matches the query, how popular the game is, how recent.

relevance = title match (up to 140) + popularity (up to 35) + release year (up to 10)

An exact title match always beats everything else. Among partial matches,
popularity can outweigh a slightly weaker match, and a newer release breaks ties
(for example the 2016 "Doom" over the 1993 one).
"""

import math
from datetime import datetime

from .text import norm_title

EXACT = 140  # the title is the query
PREFIX = 70  # the title starts with the query
WORDS = 50  # every query word starts some word in the title ("Brutal Doom")
CONTAINS = 25  # the query appears inside the title
TOKENS = 20  # every query word appears somewhere in the title
FUZZY = 5  # no direct match (IGDB found it anyway, e.g. a typo)

POPULARITY_WEIGHT = 35
RECENCY_WEIGHT = 10
FIRST_YEAR = 1985  # games from here or earlier get the lowest recency score
UNKNOWN_YEAR_SCORE = 0.2
LOCAL_RATING_VALUE = 25  # each rating on Gametrackr counts like 25 IGDB scores/follows


def match_score(query, title):
    q, t = norm_title(query), norm_title(title)
    if not q or not t:
        return 0.0
    if t == q:
        return EXACT
    tokens, words = q.split(), t.split()
    if t.startswith(q):
        score = PREFIX
    elif all(any(w.startswith(tok) for w in words) for tok in tokens):
        score = WORDS
    elif q in t:
        score = CONTAINS
    elif all(tok in t for tok in tokens):
        score = TOKENS
    else:
        score = FUZZY
    # Shorter titles are closer matches ("Doom" before "Doom Eternal: The Ancient Gods").
    return score - min(max(len(t) - len(q), 0), 40) * 0.15


def popularity_score(count):
    """0 to 1 on a log scale: about 10,000 scores/follows counts as fully popular."""
    return min(1.0, math.log10(1 + max(count, 0)) / 4)


def recency_score(year, this_year=None):
    """0 to 1: newer releases score higher; unknown years are slightly below average."""
    if not year:
        return UNKNOWN_YEAR_SCORE
    this_year = this_year or datetime.now().year
    return max(0.0, min(1.0, (year - FIRST_YEAR) / (this_year - FIRST_YEAR)))


def relevance(query, title, year=None, igdb_popularity=0, local_ratings=0):
    popularity = igdb_popularity + LOCAL_RATING_VALUE * local_ratings
    return (
        match_score(query, title)
        + POPULARITY_WEIGHT * popularity_score(popularity)
        + RECENCY_WEIGHT * recency_score(year)
    )


def rank(query, results):
    """Sort search results best-first; ties fall back to alphabetical."""
    return sorted(
        results,
        key=lambda r: (
            -relevance(query, r["title"], r["year"], r["popularity"], r["n"]),
            r["title"].lower(),
        ),
    )


# Ways to order the search results page (the first is the default).
SORTS = {
    "relevance": "Relevance",
    "newest": "Release date: newest first",
    "oldest": "Release date: oldest first",
}


def sort_results(results, sort):
    """Reorder ranked results by release date. Games with no known date go last,
    and games from the same year keep their relevance order."""
    if sort == "newest":
        return sorted(results, key=lambda r: (r["year"] is None, -(r["year"] or 0)))
    if sort == "oldest":
        return sorted(results, key=lambda r: (r["year"] is None, r["year"] or 0))
    return results
