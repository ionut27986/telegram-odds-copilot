"""OddX2 3.2: read-only basketball/hockey historical game and settlement shape probe.
No Telegram posts, bets, or database writes. Never prints API keys.
"""
import os
from datetime import datetime, timedelta, timezone
import requests

SPORTS = {
    'basketball': ('https://v1.basketball.api-sports.io', 'API_BASKETBALL_KEY'),
    'hockey': ('https://v1.hockey.api-sports.io', 'API_HOCKEY_KEY'),
}

def get(base, key, endpoint, params):
    r = requests.get(f'{base}/{endpoint}', headers={'x-apisports-key': key}, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get('errors'):
        raise RuntimeError(str(data['errors'])[:180])
    return data.get('response', [])

def main():
    print('OddX2 3.2 | READ-ONLY historical scores and settlement probe')
    print('UTC:', datetime.now(timezone.utc).isoformat())
    today = datetime.now(timezone.utc).date()
    for sport, (base, env) in SPORTS.items():
        print('\n==========', sport.upper(), '==========')
        key = os.getenv(env, '').strip()
        if not key:
            print('Missing environment variable:', env)
            continue
        finished = []
        for days_ago in (1, 2, 3, 4):
            date = (today - timedelta(days=days_ago)).isoformat()
            try:
                games = get(base, key, 'games', {'date': date})
                print(date, 'games:', len(games))
            except Exception as exc:
                print(date, 'ERROR:', type(exc).__name__, str(exc)[:140])
                continue
            for game in games:
                status = game.get('status') or {}
                short = str(status.get('short', '')) if isinstance(status, dict) else str(status)
                long = str(status.get('long', '')) if isinstance(status, dict) else ''
                if short in ('FT', 'AOT', 'AP', 'Finished') or 'finish' in long.lower() or 'after overtime' in long.lower() or 'after penalties' in long.lower():
                    finished.append(game)
            if len(finished) >= 4:
                break
        print('finished games sampled:', min(4, len(finished)))
        for game in finished[:4]:
            gid = game.get('id')
            teams = game.get('teams') or {}
            scores = game.get('scores') or {}
            print('game:', gid, 'teams:', (teams.get('home') or {}).get('name'), 'vs', (teams.get('away') or {}).get('name'))
            print(' status:', game.get('status'))
            print(' scores:', scores)
            print(' periods:', game.get('periods', '<not provided>'))
            try:
                detail = get(base, key, 'games', {'id': gid})
                print(' games?id count:', len(detail), 'detail status:', (detail[0].get('status') if detail else None))
                if detail:
                    print(' detail scores:', detail[0].get('scores'), 'periods:', detail[0].get('periods'))
            except Exception as exc:
                print(' games?id ERROR:', type(exc).__name__, str(exc)[:160])
            home_id = (teams.get('home') or {}).get('id')
            if home_id:
                try:
                    recent = get(base, key, 'games', {'team': home_id, 'last': 5})
                    print(' games?team&last count:', len(recent), 'sample statuses:', [g.get('status') for g in recent[:2]])
                except Exception as exc:
                    print(' games?team&last ERROR:', type(exc).__name__, str(exc)[:160])
            print(' ---')
    print('\nDone. Do not interpret score fields as regulation or OT-inclusive until confirmed.')

if __name__ == '__main__':
    main()
