"""Gametrackr entry point.

Run locally:   python app.py
Production:    gunicorn app:app
"""

from gametrackr import app

if __name__ == "__main__":
    app.run(debug=True)
