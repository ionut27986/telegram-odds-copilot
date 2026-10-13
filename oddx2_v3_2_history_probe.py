"""OddX2 read-only season-aware history probe. Does not alter bot, DB, or Telegram."""
import os
from datetime import datetime, timedelta, timezone
import requests

SPORTS = {'basketball': ('https://v1.basketball.api-sports.io', 'API_BASKETBALL_KEY'),
          'hockey': ('https://v1.hockey.api-sports.io', 'API_HOCKEY_KEY')}

def get(base, key, endpoint, params):
    r = requests.get(base + '/' + endpoint, headers={'x-apisports-key': key}, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get('errors'):
        raise ValueError(str(data['errors'])[:200])
    return data.get('response', [])

def main():
    print('OddX2 | season-aware historical sample | READ ONLY')
    today = datetime.now(timezone.utc).date()
    for sport, (base, env) in SPORTS.items():
        print('\n###', sport.upper())
        key = os.getenv(env, '').strip()
        if not key:
            print('MISSING', env); continue
        sample = None
        for delta in range(1, 8):
            games = get(base, key, 'games', {'date': (today - timedelta(days=delta)).isoformat()})
            sample = next((g for g in games if (g.get('status') or {}).get('short') in ('FT','AOT','AP')
                           and (g.get('teams') or {}).get('home', {}).get('id')
                           and (g.get('league') or {}).get('season')), None)
            if sample: break
        if not sample:
            print('No finished game with season found'); continue
        league = sample['league']; team = sample['teams']['home']
        print('league:', {k: league.get(k) for k in ('id','name','season')})
        print('team:', team.get('name'), 'id:', team.get('id'))
        for params in ({'team': team['id'], 'season': league['season']},
                       {'team': team['id'], 'season': league['season'], 'league': league['id']}):
            try:
                games = get(base, key, 'games', params)
                finished = [g for g in games if (g.get('status') or {}).get('short') in ('FT','AOT','AP')]
                print('query fields:', list(params), 'games:', len(games), 'finished:', len(finished))
                for g in finished[-3:]:
                    print('  id:', g.get('id'), 'date:', g.get('date'), 'status:', (g.get('status') or {}).get('short'),
                          'scores:', g.get('scores'), 'periods:', g.get('periods'))
            except Exception as exc:
                print('query failed:', type(exc).__name__, str(exc)[:200])
    print('\nDone. No writes, no picks, no messages.')

if __name__ == '__main__':
    main()
