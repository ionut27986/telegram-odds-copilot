"""OddX2 3.0 API-Sports connectivity and market availability check.
Read-only. No Telegram messages or betting actions.
Run in Railway environment: python oddx2_api_check.py
"""
import os
import requests
from datetime import datetime, timezone

SPORTS = {
    'football': ('https://v3.football.api-sports.io', 'API_FOOTBALL_KEY'),
    'basketball': ('https://v1.basketball.api-sports.io', 'API_BASKETBALL_KEY'),
    'hockey': ('https://v1.hockey.api-sports.io', 'API_HOCKEY_KEY'),
}

def request(base, key, path, params=None):
    response = requests.get(f'{base}/{path}', headers={'x-apisports-key': key}, params=params or {}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get('errors'):
        raise RuntimeError(str(data['errors']))
    return data

def main():
    print('OddX2 3.0 | API-Sports read-only check')
    print('UTC:', datetime.now(timezone.utc).isoformat())
    for sport, (base, env) in SPORTS.items():
        print(f'\n[{sport.upper()}]')
        key = os.getenv(env)
        if not key:
            print(f'MISSING ENV: {env}')
            continue
        try:
            status = request(base, key, 'status')
            print('STATUS OK; response keys:', ', '.join(status.keys()))
        except Exception as exc:
            print('STATUS FAILED:', type(exc).__name__, str(exc)[:180])
            continue
        if sport == 'football':
            continue
        try:
            leagues = request(base, key, 'leagues')
            items = leagues.get('response', [])
            print('LEAGUES:', len(items))
            for item in items[:3]:
                print('SAMPLE:', {k:item.get(k) for k in ('id','name','type')})
        except Exception as exc:
            print('LEAGUES FAILED:', type(exc).__name__, str(exc)[:180])
        try:
            bets = request(base, key, 'odds/bets')
            items = bets.get('response', [])
            print('ODDS MARKET TYPES:', len(items))
            for item in items[:5]:
                print('MARKET SAMPLE:', {k:item.get(k) for k in ('id','name')})
        except Exception as exc:
            print('ODDS MARKET LIST UNAVAILABLE:', type(exc).__name__, str(exc)[:180])
    print('\nDone. This validates connectivity only; market availability per game needs further testing.')

if __name__ == '__main__':
    main()
