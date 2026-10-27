
"""OddPlus private read-only API for published picks."""

import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DB_PATH = os.getenv("ODDS_DB_PATH", "/data/odds_copilot.sqlite3")
TOKEN = os.getenv("ODDPLUS_INTERNAL_TOKEN", "")
PORT = int(os.getenv("ODDPLUS_INTERNAL_PORT", "8090"))


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        supplied = self.headers.get("X-Internal-Token", "")

        if not TOKEN or not hmac.compare_digest(supplied, TOKEN):
            return self.respond(401, {"error": "Unauthorized"})

        if urlsplit(self.path).path != "/internal/picks":
            return self.respond(404, {"error": "Not found"})

        try:
            uri = f"file:{DB_PATH}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute("""
                    SELECT
                        id, fixture_id, published_at, kickoff,
                        league, home, away, bookmaker,
                        market, selection, odd,
                        estimated, implied, estimated_ev,
                        status, home_goals, away_goals
                    FROM direct_picks_55
                    WHERE published_at IS NOT NULL
                    ORDER BY published_at DESC, id DESC
                    LIMIT 100
                """).fetchall()

            return self.respond(200, {
                "version": "5.5",
                "count": len(rows),
                "picks": [dict(row) for row in rows]
            })

        except sqlite3.Error:
            return self.respond(503, {"error": "Data unavailable"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("ODDPLUS_INTERNAL_TOKEN is missing")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OddPlus internal API listening on port {PORT}", flush=True)
    server.serve_forever()
