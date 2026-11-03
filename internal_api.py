"""OddPlus API: public locked feed and protected internal diagnostics.

The public feed contains no match, market, odds, or prediction details.
Unlocking requires a separate server-verified rewarded-ad implementation.
"""
import hashlib
import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DB_PATH = os.getenv('ODDS_DB_PATH', '/data/odds_copilot.sqlite3')
TOKEN = os.getenv('ODDPLUS_INTERNAL_TOKEN', '')
PORT = int(os.getenv('ODDPLUS_INTERNAL_PORT', '8090'))


def database():
    return sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=5)


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def opaque_id(fixture_id):
    return hmac.new(TOKEN.encode(), str(fixture_id).encode(), hashlib.sha256).hexdigest()[:32]


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get('X-Internal-Token', '')
        return bool(TOKEN) and hmac.compare_digest(supplied, TOKEN)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/health':
            return self.respond(200, {'status': 'ok', 'service': 'oddplus-api'})

        if path == '/v1/feed':
            try:
                with database() as db:
                    if not table_exists(db, 'direct_picks_57'):
                        return self.respond(200, {'version': '1', 'count': 0, 'items': []})
                    rows = db.execute('''
                        SELECT fixture_id, MAX(published_at) AS latest
                        FROM direct_picks_57
                        WHERE published_at IS NOT NULL
                        GROUP BY fixture_id
                        ORDER BY latest DESC
                        LIMIT 100
                    ''').fetchall()
                items = [{'id': opaque_id(row[0]), 'locked': True,
                          'label': 'Analiză nouă disponibilă'} for row in rows]
                return self.respond(200, {'version': '1', 'count': len(items), 'items': items})
            except sqlite3.Error:
                return self.respond(503, {'error': 'Data unavailable'})

        if not self.authorized():
            return self.respond(401, {'error': 'Unauthorized'})

        if path not in ('/internal/picks', '/internal/picks57'):
            return self.respond(404, {'error': 'Not found'})

        try:
            with database() as db:
                db.row_factory = sqlite3.Row
                if path == '/internal/picks':
                    table = 'direct_picks_55'
                    fields = '''id, fixture_id, published_at, kickoff, league,
                                home, away, bookmaker, market, selection, odd,
                                estimated, implied, estimated_ev, status,
                                home_goals, away_goals'''
                    version = '5.5'
                else:
                    table = 'direct_picks_57'
                    fields = '''id, fixture_id, published_at, kickoff, league,
                                home, away, bookmaker, market, selection,
                                market_kind, odd, estimated, implied,
                                estimated_ev, status, home_goals, away_goals'''
                    version = '5.7'
                if not table_exists(db, table):
                    return self.respond(200, {'version': version, 'count': 0, 'picks': []})
                rows = db.execute(
                    f'SELECT {fields} FROM {table} '
                    'WHERE published_at IS NOT NULL '
                    'ORDER BY published_at DESC, id DESC LIMIT 100'
                ).fetchall()
            return self.respond(200, {'version': version, 'count': len(rows),
                                      'picks': [dict(row) for row in rows]})
        except sqlite3.Error:
            return self.respond(503, {'error': 'Data unavailable'})

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    if not TOKEN:
        raise RuntimeError('ODDPLUS_INTERNAL_TOKEN is missing')
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'OddPlus API listening on port {PORT}', flush=True)
    server.serve_forever()
