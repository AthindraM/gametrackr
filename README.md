# Gametrackr

Track and rate the games you play.

## Run locally

    pip install -r requirements.txt
    python app.py

Optional environment variables: `STEAM_API_KEY`, `IGDB_CLIENT_ID`, `IGDB_CLIENT_SECRET`,
`SECRET_KEY`, `GAMETRACKR_DB` (database file path), `BEHIND_PROXY=1` (in production).

Production: `gunicorn app:app`

## Where things live

    app.py                 entry point (keeps `gunicorn app:app` working)
    gametrackr/
      __init__.py          creates the Flask app and wires everything together
      config.py            paths and the secret key
      db.py                database schema, connection, upgrades
      helpers.py           login protection, CSRF, template filters
      igdb.py              IGDB client and cover-image helpers
      shelves.py           shelves (Currently Playing / Completed / Wishlist) and profile shelf queries
      catalog.py           game search, linking to IGDB, merging duplicates
      ranking.py           search ranking: title match, popularity, release year (weights at the top)
      text.py              title normalizing
      auth.py              sign up / log in / log out
      games.py             home, search, game pages, add/remove ratings, shelf dropdown
      profiles.py          profile page (shelves) and the "View all" page for each shelf
      steam.py             Steam connect and library import
      settings.py          settings pages
      misc.py              favicon routes and error page
    templates/             HTML pages (Jinja)
    static/css/style.css   all styling
    static/js/             app.js (menu, autocomplete), covers.js (image retries)
    static/favicons/       icons and web manifest
