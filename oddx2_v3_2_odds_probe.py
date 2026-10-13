"""OddX2 3.2 read-only pre-match odds probe. No Telegram or DB writes.
Run: python oddx2_v3_2_odds_probe.py
Prints only response shapes and limited non-secret sample market information.
"""
import os
from datetime import datetime, timedelta, timezone
import requests

SPORTS = {
    'basketball': ('https://v1.basketball.api-sports.io', 'API_BASKETBALL_KEY'),
    'hockey': ('https://v1.hockey.api-sports.io', 'API_HOCKEY_KEY'),
}


def call(base, key, endpoint, params=None):
    try:
        response = requests.get(base + '/' + endpoint,
                                headers={'x-apisports-key': key},
                                params=params or {}, timeout=20)
        data = response.json()
        errors = data.get('errors') if isinstance(data, dict) else None
        if response.status_code >= 400 or errors:
            return None, f'HTTP {response.status_code}; errors={str(errors)[:180]}'
        return data, None
    except (requests.RequestException, ValueError) as exc:
        return None, f'{type(exc).__name__}: {str(exc)[:120]}'


def inspect(sport, base, key):
    print('\n' + '=' * 15, sport.upper(), '=' * 15)
    today = datetime.now(timezone.utc).date()
    upcoming = []
    for day in (today, today + timedelta(days=1), today + timedelta(days=2)):
        data, err = call(base, key, 'games', {'date': day.isoformat()})
        if err:
            print('games', day, err)
            continue
        games = data.get('response', [])
        print('games', day, 'count:', len(games))
        for g in games:
            raw = g.get('date')
            if not raw:
                continue
            try:
                kickoff = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if kickoff > datetime.now(timezone.utc) + timedelta(minutes=15):
                upcoming.append(g)
    print('future games sampled:', min(3, len(upcoming)))
    for game in upcoming[:3]:
        gid = game.get('id')
        league = game.get('league') or {}
        print('game id:', gid, 'league:', league.get('id'),
              'game keys:', sorted(game.keys()),
              'score keys:', sorted((game.get('scores') or {}).keys()))
        if not gid:
            continue
        for params in ({'game': gid}, {'game': gid, 'page': 1}):
            data, err = call(base, key, 'odds', params)
            if err:
                print('odds', list(params.keys()), err)
                continue
            entries = data.get('response', [])
            print('odds', list(params.keys()), 'results:', data.get('results'),
                  'response count:', len(entries), 'paging:', data.get('paging'))
            if entries:
                entry = entries[0]
                print('  entry keys:', sorted(entry.keys()))
                books = entry.get('bookmakers') or []
                print('  bookmakers:', len(books))
                for book in books[:2]:
                    print('  bookmaker:', book.get('name'), 'keys:', sorted(book.keys()))
                    for bet in (book.get('bets') or [])[:4]:
                        values = bet.get('values') or []
                        print('    market:', bet.get('id'), bet.get('name'),
                              'values:', [str(v.get('value'))[:35] for v in values[:4]],
                              'odds:', [str(v.get('odd'))[:10] for v in values[:4]])
                break


def main():
    print('OddX2 3.2 | READ-ONLY odds availability and response-shape probe')
    print('UTC:', datetime.now(timezone.utc).isoformat())
    for sport, (base, env) in SPORTS.items():
        key = os.getenv(env, '').strip()
        if not key:
            print(sport, 'missing environment variable:', env)
            continue
        inspect(sport, base, key)
    print('\nDone. No picks generated, no database changes, no Telegram messages.')


if __name__ == '__main__':
    main()
