import asyncio
import logging
import math
import sqlite3
from pathlib import Path
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import combinations
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger('odds-copilot')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KEY = os.getenv('API_FOOTBALL_KEY')
URL = 'https://v3.football.api-sports.io'
LOCAL = ZoneInfo('Europe/Bucharest')
DB_PATH = Path(os.getenv('ODDS_DB_PATH', '/data/odds_copilot.sqlite3'))
AUTO_SCAN_HOURS = 2
SCAN_LIMIT_PER_DAY = 16
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
VERSION = "5.4-direct-ev-diagnostics"
MIN_LEAD_MINUTES = 15
DAILY_SINGLE_TARGET = 20
MAX_NEW_SINGLES_PER_SCAN = 5
# Legacy constants retained only for dormant historical functions.
DAILY_DOUBLE_TARGET = 10
MAX_NEW_DOUBLES_PER_SCAN = 3


def api(path, params):
    response = requests.get(f'{URL}/{path}', headers={'x-apisports-key': KEY}, params=params, timeout=20)
    response.raise_for_status()
    result = response.json()
    if result.get('errors'):
        raise ValueError(str(result['errors']))
    return result.get('response', [])


def fixtures(hours=6):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    found = []
    for day in sorted({now.date().isoformat(), end.date().isoformat()}):
        for game in api('fixtures', {'date': day, 'timezone': 'UTC'}):
            stamp = game.get('fixture', {}).get('date')
            if not stamp:
                continue
            kick = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)
            if now <= kick <= end:
                found.append((kick, game))
    return sorted(found, key=lambda x: x[0])


def odds_for(kick, game):
    fix, league = game['fixture'], game['league']
    if not fix.get('id') or not league.get('id') or league.get('season') is None:
        return []
    return api('odds', {'fixture': fix['id'], 'league': league['id'],
                        'season': league['season'], 'date': kick.date().isoformat()})


def recent_games(team_id, cache):
    if team_id in cache:
        return cache[team_id]
    records = []
    for game in api('fixtures', {'team': team_id, 'last': 15}):
        sides, goals = game.get('teams', {}), game.get('goals', {})
        home, away = sides.get('home', {}), sides.get('away', {})
        gh, ga = goals.get('home'), goals.get('away')
        if gh is None or ga is None:
            continue
        if home.get('id') == team_id:
            records.append({'venue': 'home', 'for': gh, 'against': ga, 'total': gh + ga,
                            'btts': gh > 0 and ga > 0})
        elif away.get('id') == team_id:
            records.append({'venue': 'away', 'for': ga, 'against': gh, 'total': gh + ga,
                            'btts': ga > 0 and gh > 0})
    cache[team_id] = records
    return records


def exact_goal_market(market_id, label):
    """Only whole/half goal lines with binary settlement; exclude quarter Asian lines."""
    if market_id == 8 and label.lower() in ('yes', 'no'):
        return ('btts', label.lower() == 'yes', None)
    if market_id != 5:
        return None
    match = re.fullmatch(r'(Over|Under)\s+(\d+(?:\.\d+)?)', label, flags=re.I)
    if not match:
        return None
    threshold = float(match.group(2))
    # Half-goal totals settle without push, unlike integer and quarter-goal lines.
    if abs(threshold % 1 - 0.5) > 1e-8:
        return None
    return ('goals', match.group(1).lower() == 'over', threshold)


def sample_frequency(games, market):
    kind, positive, threshold = market
    if kind == 'btts':
        hits = sum(g['btts'] == positive for g in games)
    else:
        hits = sum((g['total'] > threshold) == positive for g in games)
    return hits, len(games)


def poisson_cdf(k, lam):
    return sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k + 1))


def goal_probability(market, home_lambda, away_lambda):
    kind, positive, threshold = market
    if kind == 'goals':
        under = poisson_cdf(math.floor(threshold), home_lambda + away_lambda)
        return 1 - under if positive else under
    yes = (1 - math.exp(-home_lambda)) * (1 - math.exp(-away_lambda))
    return yes if positive else 1 - yes


# Competition identifiers are API-Football league IDs; do not infer from league names alone.
# UEFA: Champions League 2, Europa League 3, Conference League 848.
# Domestic: Premier League 39, La Liga 140, Bundesliga 78, Serie A 135, Ligue 1 61.
TOP_UEFA_LEAGUES = {2, 3, 848}
TOP_DOMESTIC_LEAGUES = {39, 140, 78, 135, 61}
FOOTBALL_SCAN_BUDGET = 120
FOOTBALL_PRIORITY_RESERVE = 80


def league_label(game):
    league = game.get('league') or {}
    return f"{league.get('name', '?')} ({league.get('id', '?')})"


def football_priority(game):
    league = game.get('league') or {}
    league_id = league.get('id')
    try:
        league_id = int(league_id)
    except (ValueError, TypeError):
        return 2
    if league_id in TOP_UEFA_LEAGUES:
        return 0
    if league_id in TOP_DOMESTIC_LEAGUES:
        return 1
    return 2


def prioritize_football_fixtures(games):
    """Equal-priority UEFA and Top 5 coverage, with space for other leagues.

    Never promote a competition merely because it has many quoted markets.
    The remaining general slots are filled chronologically. All fixtures are
    unique, and priority never bypasses the statistical eligibility filters.
    """
    uefa = sorted((item for item in games if football_priority(item[1]) == 0), key=lambda item: item[0])
    top5 = sorted((item for item in games if football_priority(item[1]) == 1), key=lambda item: item[0])
    other = sorted((item for item in games if football_priority(item[1]) == 2), key=lambda item: item[0])

    # Alternate between the two equal-priority pools, rather than filling UEFA first.
    preferred = []
    for i in range(max(len(uefa), len(top5))):
        if i < len(uefa):
            preferred.append(uefa[i])
        if i < len(top5):
            preferred.append(top5[i])
    selected = preferred[:FOOTBALL_PRIORITY_RESERVE]
    selected.extend(other[:FOOTBALL_SCAN_BUDGET - len(selected)])
    if len(selected) < FOOTBALL_SCAN_BUDGET:
        selected.extend(preferred[FOOTBALL_PRIORITY_RESERVE:
                                  FOOTBALL_PRIORITY_RESERVE + FOOTBALL_SCAN_BUDGET - len(selected)])
    return selected


def analyze_batch(max_doubles=3, audit=None, singles_mode=False):
    """Rank multiple independent experimental football doubles."""
    cache, choices = {}, []
    inspected, qualified = 0, 0
    all_fixtures = fixtures(20)
    prioritized = prioritize_football_fixtures(all_fixtures)
    if audit is not None:
        audit['total'] = len(all_fixtures)
        audit['selected'] = len(prioritized)
        audit['leagues'] = {}
        for kick, game in all_fixtures:
            name = league_label(game)
            entry = audit['leagues'].setdefault(name, {'available': 0, 'selected': 0, 'checked': 0, 'history': 0, 'odds': 0, 'edge': 0})
            entry['available'] += 1
        for kick, game in prioritized:
            audit['leagues'][league_label(game)]['selected'] += 1
    for kick, game in prioritized:
        if kick <= datetime.now(timezone.utc) + timedelta(minutes=MIN_LEAD_MINUTES + 10):
            continue
        teams = game.get('teams', {})
        h, a = teams.get('home', {}), teams.get('away', {})
        if not h.get('id') or not a.get('id'):
            continue
        inspected += 1
        row = audit['leagues'][league_label(game)] if audit is not None else None
        if row is not None:
            row['checked'] += 1
        try:
            prices = odds_for(kick, game)
            home = [g for g in recent_games(h['id'], cache) if g['venue'] == 'home'][:10]
            away = [g for g in recent_games(a['id'], cache) if g['venue'] == 'away'][:10]
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning('Analysis unavailable: %s', exc)
            continue
        if len(home) < 5 or len(away) < 5:
            continue
        qualified += 1
        if row is not None:
            row['history'] += 1
            if prices:
                row['odds'] += 1
        raw_home = (sum(g['for'] for g in home) / len(home) + sum(g['against'] for g in away) / len(away)) / 2
        raw_away = (sum(g['for'] for g in away) / len(away) + sum(g['against'] for g in home) / len(home)) / 2
        home_lambda = max(0.15, min(0.8 * raw_home + 0.2 * 1.40, 4.0))
        away_lambda = max(0.15, min(0.8 * raw_away + 0.2 * 1.15, 4.0))
        for entry in prices:
            for bookmaker in entry.get('bookmakers', []):
                for bet in bookmaker.get('bets', []):
                    for outcome in bet.get('values', []):
                        label = str(outcome.get('value', '')).strip()
                        market = exact_goal_market(bet.get('id'), label)
                        if market is None:
                            continue
                        try:
                            odd = float(outcome.get('odd'))
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(odd) or not (1.25 <= odd <= (3.00 if singles_mode else 1.85)):
                            continue
                        estimated = goal_probability(market, home_lambda, away_lambda)
                        implied = 1 / odd
                        difference = estimated - implied
                        if difference < 0.05:
                            continue
                        if row is not None:
                            row['edge'] += 1
                        choices.append({
                            'fixture': game['fixture']['id'], 'kick': kick,
                            'home': h.get('name', '?'), 'away': a.get('name', '?'),
                            'bookmaker': bookmaker.get('name', '?'),
                            'market': bet.get('name', '?'), 'market_kind': market[0],
                            'positive': market[1], 'threshold': market[2],
                            'selection': label, 'odd': odd,
                            'estimated': estimated, 'implied': implied,
                            'difference': difference,
                            'home_lambda': home_lambda, 'away_lambda': away_lambda,
                            'home_n': len(home), 'away_n': len(away),
                            'league': league_label(game),
                        })
    unique = {}
    for c in choices:
        key = (c['fixture'], c['market'], c['selection'], c['bookmaker'])
        if key not in unique or c['odd'] > unique[key]['odd']:
            unique[key] = c
    if singles_mode:
        # No global top-150 cap before filtering already published events.
        # Expected value is only a model estimate, not demonstrated profitability.
        return sorted(unique.values(), key=lambda c: (c['estimated']*c['odd']-1, c['difference']), reverse=True), inspected, qualified, len(unique)
    choices = sorted(unique.values(), key=lambda c: c['difference'], reverse=True)[:150]
    if audit is not None:
        audit['unique_quotes'] = len(choices)
        audit['before_reuse'] = len(choices)
    with db_connect() as con:
        used = {r[0] for r in con.execute('SELECT fixture_id FROM legs')}
    choices = [c for c in choices if c['fixture'] not in used]
    if audit is not None:
        audit['reused_quotes'] = audit['before_reuse'] - len(choices)
        audit['distinct_events'] = len({c['fixture'] for c in choices})
        audit['bookmakers'] = len({c['bookmaker'] for c in choices})
        audit['pair_filters'] = {'same_event': 0, 'different_bookmaker': 0,
                                 'outside_target': 0, 'qualifying': 0}
        audit['data_quality'] = {'minimum_5_to_6': 0, 'moderate_7_to_9': 0,
                                 'strong_10_plus': 0}
        for c in choices:
            sample = min(c['home_n'], c['away_n'])
            tier = ('minimum_5_to_6' if sample <= 6 else
                    'moderate_7_to_9' if sample <= 9 else 'strong_10_plus')
            audit['data_quality'][tier] += 1
    ranked = []
    for x, y in combinations(choices, 2):
        if x['fixture'] == y['fixture']:
            if audit is not None:
                audit['pair_filters']['same_event'] += 1
            continue
        if x['bookmaker'] != y['bookmaker']:
            if audit is not None:
                audit['pair_filters']['different_bookmaker'] += 1
            continue
        combined = x['odd'] * y['odd']
        if not 1.90 <= combined <= 2.10:
            if audit is not None:
                audit['pair_filters']['outside_target'] += 1
            continue
        if audit is not None:
            audit['pair_filters']['qualifying'] += 1
        score = x['difference'] + y['difference'] - 0.1 * abs(combined - 2)
        ranked.append((score, x, y, combined))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected, reserved = [], set()
    for _, x, y, combined in ranked:
        if x['fixture'] in reserved or y['fixture'] in reserved:
            continue
        selected.append((x, y, combined))
        reserved.update((x['fixture'], y['fixture']))
        if len(selected) >= max_doubles:
            break
    if audit is not None:
        audit['ranked_pairs'] = len(ranked)
        audit['selected_pairs'] = len(selected)
        audit['top_pairs'] = [
            {'a': f"{x['home']} vs {x['away']}", 'b': f"{y['home']} vs {y['away']}",
             'bookmaker': x['bookmaker'], 'combined': total,
             'gap': x['difference'] + y['difference'],
             'samples': (min(x['home_n'], x['away_n']), min(y['home_n'], y['away_n']))}
            for _, x, y, total in ranked[:5]
        ]
    return selected, inspected, qualified, len(choices)


def analyze():
    doubles, inspected, qualified, count = analyze_batch(1)
    return (doubles[0] if doubles else None), inspected, qualified, count


def find_double():
    candidates, inspected = [], 0
    for kick, game in fixtures(4)[:20]:
        inspected += 1
        try:
            prices = odds_for(kick, game)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning('No odds: %s', exc)
            continue
        for entry in prices:
            for book in entry.get('bookmakers', []):
                for bet in book.get('bets', []):
                    if bet.get('id') != 1:
                        continue
                    for val in bet.get('values', []):
                        try:
                            odd = float(val.get('odd'))
                        except (TypeError, ValueError):
                            continue
                        if 1.25 <= odd <= 1.75:
                            candidates.append({'fixture': game['fixture']['id'], 'kick': kick,
                                               'home': game['teams']['home']['name'],
                                               'away': game['teams']['away']['name'],
                                               'bookmaker': book.get('name', '?'),
                                               'market': bet.get('name', '?'),
                                               'selection': val.get('value', '?'), 'odd': odd})
    best, distance = None, float('inf')
    for x, y in combinations(candidates, 2):
        if x['fixture'] == y['fixture'] or x['bookmaker'] != y['bookmaker']:
            continue
        combined = x['odd'] * y['odd']
        if 1.90 <= combined <= 2.10 and abs(combined - 2) < distance:
            best, distance = (x, y, combined), abs(combined - 2)
    return best, inspected


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 OddX2 5.0-beta online!\n/status - status\n/fixtures - upcoming fixtures\n/odds - bookmaker odds\n/double - mathematical test\n/analyze - statistical model\n/history - saved doubles\n/stats - results and ROI\n/results - refresh scores\n/scan - scan and publish\n/report - daily report\n/weekly - last completed week\n/monthly - last completed month\n/alltime - published track record\n/diagnostics - scan reasons\n/footballaudit - football coverage\n/pairaudit - football pair filters\n/sports - check basketball and hockey feeds\n/basketball - basketball research preview\n/hockey - hockey research preview\n/multiscan - multisport diagnostic research')


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('✅ OddX2 5.0-beta is online.')


async def meciuri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 Checking fixtures...')
    try:
        games = await asyncio.to_thread(fixtures, 2)
        lines = [f'⚽ Fixtures in next 2 hours: {len(games)}']
        for kick, game in games[:30]:
            lines.append(f"{kick.astimezone(LOCAL):%H:%M} {game['teams']['home']['name']} vs {game['teams']['away']['name']}")
        await update.message.reply_text('\n'.join(lines)[:4000])
    except Exception:
        log.exception('Fixtures failed')
        await update.message.reply_text('❌ API error. Check Railway logs.')


async def cote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 Checking bookmaker odds...')
    try:
        games = await asyncio.to_thread(fixtures, 2)
        lines = ['📊 Sample odds (not recommendations):']
        for kick, game in games[:5]:
            for entry in await asyncio.to_thread(odds_for, kick, game):
                for book in entry.get('bookmakers', [])[:1]:
                    bet = next((b for b in book.get('bets', []) if b.get('id') == 1), None)
                    if bet:
                        values = ', '.join(f"{v.get('value')}: {v.get('odd')}" for v in bet.get('values', []))
                        lines.append(f"{kick.astimezone(LOCAL):%H:%M} {game['teams']['home']['name']} vs {game['teams']['away']['name']}\n{book.get('name')}: {values}")
        await update.message.reply_text('\n\n'.join(lines)[:4000])
    except Exception:
        log.exception('Odds failed')
        await update.message.reply_text('❌ API error. Check Railway logs.')


async def dubla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 Searching mathematical double...')
    try:
        best, count = await asyncio.to_thread(find_double)
        if not best:
            await update.message.reply_text(f'No qualifying 1X2 double among {count} fixtures.')
            return
        x, y, total = best
        lines = ['🧪 Mathematical double (not analysis):']
        for c in (x, y):
            lines.append(f"{c['home']} vs {c['away']} — {c['kick'].astimezone(LOCAL):%H:%M}\n{c['selection']} @ {c['odd']:.2f}")
        lines.append(f"Bookmaker: {x['bookmaker']}\nCombined: {total:.2f}\n⚠️ No statistical forecast.")
        await update.message.reply_text('\n\n'.join(lines))
    except Exception:
        log.exception('Double failed')
        await update.message.reply_text('❌ Error. Check Railway logs.')


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=20)
    con.execute('PRAGMA busy_timeout=20000')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS doubles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL UNIQUE,
        bookmaker TEXT NOT NULL,
        combined_odds REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        double_id INTEGER NOT NULL REFERENCES doubles(id),
        fixture_id INTEGER NOT NULL,
        kickoff TEXT NOT NULL,
        home TEXT NOT NULL, away TEXT NOT NULL,
        market TEXT NOT NULL, selection TEXT NOT NULL,
        market_kind TEXT NOT NULL, positive INTEGER NOT NULL,
        threshold REAL,
        odd REAL NOT NULL, estimated REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        home_goals INTEGER, away_goals INTEGER
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS scan_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at TEXT NOT NULL,
        status TEXT NOT NULL,
        checked INTEGER NOT NULL DEFAULT 0,
        qualified INTEGER NOT NULL DEFAULT 0,
        candidates INTEGER NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT ''
    )''')
    # Add publication state without resetting historical research data.
    for column in ('published_at', 'result_posted_at'):
        try:
            con.execute(f'ALTER TABLE doubles ADD COLUMN {column} TEXT')
        except sqlite3.OperationalError as exc:
            if 'duplicate column name' not in str(exc).lower():
                raise
    con.execute('''CREATE TABLE IF NOT EXISTS report_posts (
        period_type TEXT NOT NULL, period_key TEXT NOT NULL,
        sent_at TEXT NOT NULL, PRIMARY KEY(period_type,period_key))''')
    con.execute('''CREATE TABLE IF NOT EXISTS sport_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
        sport TEXT NOT NULL, endpoint TEXT NOT NULL, item_count INTEGER NOT NULL,
        sample TEXT NOT NULL DEFAULT '', status TEXT NOT NULL)''')
    con.commit()
    return con



# 5.0 uses an independent ledger. Legacy doubles and their historical results
# are retained intact but are no longer posted or included in the new ROI.
def singles_db():
    con = db_connect()
    con.execute('''CREATE TABLE IF NOT EXISTS value_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        published_at TEXT,
        result_posted_at TEXT,
        fixture_id INTEGER NOT NULL UNIQUE,
        kickoff TEXT NOT NULL,
        league TEXT NOT NULL,
        home TEXT NOT NULL, away TEXT NOT NULL,
        bookmaker TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
        market_kind TEXT NOT NULL, positive INTEGER NOT NULL, threshold REAL,
        odd REAL NOT NULL, estimated REAL NOT NULL, implied REAL NOT NULL,
        estimated_ev REAL NOT NULL, home_n INTEGER NOT NULL, away_n INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        home_goals INTEGER, away_goals INTEGER
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS value_scan_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at TEXT NOT NULL,
        status TEXT NOT NULL, inspected INTEGER NOT NULL DEFAULT 0,
        history_ok INTEGER NOT NULL DEFAULT 0, candidates INTEGER NOT NULL DEFAULT 0,
        saved INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
    )''')
    con.commit()
    return con


# 5.1: read-only contextual review. Missing data is explicitly UNKNOWN.
# This is not a calibrated adjustment to predicted probabilities.
def inspect_match_context(fixture_id):
    result = {'fixture_id': fixture_id, 'warnings': [], 'unknown': [], 'details': []}
    try:
        matches = api('fixtures', {'id': fixture_id})
        if not matches:
            result['unknown'].append('Fixture details unavailable')
            return result
        game = matches[0]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        result['unknown'].append(f'Fixture lookup failed ({type(exc).__name__})')
        return result
    fixture = game.get('fixture') or {}
    teams = game.get('teams') or {}
    home = teams.get('home') or {}
    away = teams.get('away') or {}
    result['details'].append(f"Fixture: {home.get('name', '?')} vs {away.get('name', '?')}")
    venue = fixture.get('venue') or {}
    if venue.get('name'):
        result['details'].append(f"Listed venue: {venue['name']} ({venue.get('city') or 'city unknown'})")
        result['unknown'].append('Neutral venue not independently confirmed')
    else:
        result['unknown'].append('Venue unavailable; neutral status unknown')
    try:
        injuries = api('injuries', {'fixture': fixture_id})
        if not isinstance(injuries, list):
            raise ValueError('Unexpected injuries response')
        relevant = []
        for entry in injuries:
            if not isinstance(entry, dict):
                continue
            player = entry.get('player') or {}
            team = entry.get('team') or {}
            reason = player.get('reason') or player.get('type') or 'reason not supplied'
            relevant.append(f"{team.get('name', '?')}: {player.get('name', '?')} ({reason})")
        if relevant:
            result['warnings'].append(f'{len(relevant)} reported absences; player importance NOT assessed')
            result['details'].extend(relevant[:8])
            if len(relevant)>8:
                result['details'].append(f'...and {len(relevant)-8} more')
        else:
            result['unknown'].append('No injuries returned; coverage/completeness not verified')
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        result['unknown'].append(f'Injuries unavailable ({type(exc).__name__})')
    try:
        lineups = api('fixtures/lineups', {'fixture': fixture_id})
        if not isinstance(lineups, list):
            raise ValueError('Unexpected lineups response')
        valid = [v for v in lineups if isinstance(v,dict) and isinstance(v.get('startXI'),list) and v['startXI']]
        if len(valid) == 2:
            for entry in valid:
                team_name = (entry.get('team') or {}).get('name', '?')
                names = [(p.get('player') or {}).get('name','?') for p in entry['startXI']]
                result['details'].append(f'{team_name} confirmed XI ({len(names)}): '+', '.join(names[:11]))
            result['unknown'].append('Rotation severity unknown without a validated expected-XI baseline')
        else:
            result['unknown'].append('Both confirmed starting XIs not available yet')
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        result['unknown'].append(f'Lineups unavailable ({type(exc).__name__})')
    result['unknown'].append('Team motivation / qualification incentives not independently verified')
    result['unknown'].append('Weather and travel load not checked')
    return result


def context_audit_text(limit=8):
    with singles_db() as con:
        rows = con.execute("""SELECT id,fixture_id,home,away,kickoff
            FROM value_picks WHERE published_at IS NOT NULL
            ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return 'No published individual picks to audit yet.'
    lines = ['🩺 OddX2 5.1 | MATCH CONTEXT AUDIT',
             'READ-ONLY. Existing picks, odds, results and ROI unchanged.',
             'Observational only; missing information does not mean no risk.']
    for id_, fixture_id, home, away, kickoff in rows:
        info = inspect_match_context(fixture_id)
        lines.append(f'\n#{id_} {home} vs {away} | '+
                     ('PAST KICKOFF' if datetime.fromisoformat(kickoff).astimezone(timezone.utc) <= datetime.now(timezone.utc) else 'UPCOMING'))
        lines.extend('⚠️ '+w for w in info['warnings'])
        lines.extend('• '+d for d in info['details'][:6])
        lines.append('Unknown: '+ '; '.join(info['unknown'][:5]))
    lines.append('No automated exclusion or model probability adjustment in 5.1.')
    return '\n'.join(lines)


async def contextaudit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🩺 OddX2 5.1 | Checking match context (read-only)...')
    try:
        report = await asyncio.to_thread(context_audit_text, 5)
        for start in range(0, len(report), 3800):
            await update.message.reply_text(report[start:start+3800])
    except Exception:
        log.exception('Context audit failed')
        await update.message.reply_text('Context audit unavailable. Check Railway logs; picks unchanged.')


def select_value_picks(limit=5):
    quotes, inspected, history_ok, candidates = analyze_batch(singles_mode=True)
    with singles_db() as con:
        old = {row[0] for row in con.execute('SELECT fixture_id FROM value_picks')}
        # Legacy doubles may overlap. Their returns are tracked separately.
        old.update(row[0] for row in con.execute('''SELECT legs.fixture_id FROM legs JOIN doubles ON doubles.id=legs.double_id WHERE doubles.published_at IS NOT NULL'''))
    seen = set()
    selected = []
    for quote in quotes:
        event = quote['fixture']
        if event in old or event in seen:
            continue
        # Avoid multiple highly correlated bets from one event.
        seen.add(event)
        selected.append(quote)
        if len(selected) >= limit:
            break
    return selected, inspected, history_ok, candidates


def value_scan():
    now = datetime.now(timezone.utc)
    today = now.astimezone(LOCAL).date()
    with singles_db() as con:
        runs = con.execute('SELECT scanned_at FROM value_scan_runs ORDER BY id DESC LIMIT 100').fetchall()
        saved = con.execute('SELECT created_at FROM value_picks').fetchall()
    n_scans = sum(datetime.fromisoformat(t).astimezone(LOCAL).date() == today for (t,) in runs)
    n_saved = sum(datetime.fromisoformat(t).astimezone(LOCAL).date() == today for (t,) in saved)
    if n_scans >= SCAN_LIMIT_PER_DAY:
        return 'daily_scan_limit_reached'
    if n_saved >= DAILY_SINGLE_TARGET:
        return 'daily_research_target_reached'
    inspected = history_ok = candidates = saved_count = 0
    try:
        picks, inspected, history_ok, candidates = select_value_picks(
            min(MAX_NEW_SINGLES_PER_SCAN, DAILY_SINGLE_TARGET-n_saved))
        with singles_db() as con:
            for c in picks:
                cur = con.execute('''INSERT OR IGNORE INTO value_picks (
                    created_at,fixture_id,kickoff,league,home,away,bookmaker,market,
                    selection,market_kind,positive,threshold,odd,estimated,implied,
                    estimated_ev,home_n,away_n
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
                    now.isoformat(), c['fixture'], c['kick'].isoformat(),c['league'],
                    c['home'],c['away'],c['bookmaker'],c['market'],c['selection'],
                    c['market_kind'],int(c['positive']),c['threshold'],c['odd'],
                    c['estimated'],c['implied'],c['estimated']*c['odd']-1,
                    c['home_n'],c['away_n']))
                saved_count += cur.rowcount
        status = f'saved_{saved_count}' if saved_count else 'no_new_qualifying_single'
        detail = 'football; one quote per event; model estimates uncalibrated'
    except Exception as exc:
        log.exception('5.0 value scan failed')
        status, detail = 'error', f'{type(exc).__name__}: {str(exc)[:160]}'
    with singles_db() as con:
        con.execute('''INSERT INTO value_scan_runs
            (scanned_at,status,inspected,history_ok,candidates,saved,detail)
            VALUES (?,?,?,?,?,?,?)''',
            (now.isoformat(),status,inspected,history_ok,candidates,saved_count,detail))
    return status


def pending_value_posts():
    now = datetime.now(timezone.utc)
    with singles_db() as con:
        rows = con.execute('''SELECT id,kickoff,created_at,league,home,away,bookmaker,
            market,selection,odd,estimated,implied,estimated_ev,home_n,away_n
            FROM value_picks WHERE published_at IS NULL ORDER BY id''').fetchall()
    valid = []
    for row in rows:
        kickoff = datetime.fromisoformat(row[1]).astimezone(timezone.utc)
        created = datetime.fromisoformat(row[2]).astimezone(timezone.utc)
        if kickoff > now+timedelta(minutes=MIN_LEAD_MINUTES) and now-created < timedelta(minutes=15):
            valid.append(row)
    return valid


async def publish_value_picks(app):
    if not CHANNEL_ID:
        return
    for row in await asyncio.to_thread(pending_value_posts):
        (id_, kick, created, league, home, away, book, market, selection,
         odd, estimated, implied, ev, hn, an) = row
        local = datetime.fromisoformat(kick).astimezone(LOCAL)
        msg = (f'📊 OddX2 | VALUE PICK #{id_}\n'
               f'{home} vs {away}\n{league}\n'
               f'Kickoff: {local:%d %b %Y, %H:%M} (Bucharest)\n'
               f'{market}: {selection} @ {odd:.2f}\nBookmaker: {book}\n'
               f'Implied probability: {implied:.1%}\n'
               f'Experimental model probability: {estimated:.1%}\n'
               f'Estimated EV: {ev:+.1%}\n'
               f'Historical sample: {hn} home / {an} away\n'
               '⚠️ Uncalibrated model; positive EV is not proven value. '
               'Odds may change. Research only, no guaranteed returns.')
        await app.bot.send_message(chat_id=CHANNEL_ID,text=msg)
        with singles_db() as con:
            con.execute('UPDATE value_picks SET published_at=? WHERE id=? AND published_at IS NULL',
                        (datetime.now(timezone.utc).isoformat(),id_))


def settle_value_picks():
    with singles_db() as con:
        rows = con.execute("SELECT id,fixture_id,market_kind,positive,threshold FROM value_picks WHERE status='pending'").fetchall()
        cache = {}
        changed = 0
        for id_, fixture, kind, positive, threshold in rows:
            if fixture not in cache:
                try:
                    games = api('fixtures', {'id': fixture})
                    cache[fixture] = games[0] if games else None
                except (requests.RequestException, ValueError):
                    log.exception('5.0 settlement lookup failed for %s', fixture)
                    cache[fixture] = None
            game = cache[fixture]
            if not game:
                continue
            status = (game.get('fixture') or {}).get('status',{}).get('short')
            if status in ('CANC','PST','ABD','AWD','WO'):
                result, hg, ag = 'void', None, None
            elif status == 'FT':
                goals = game.get('goals') or {}
                hg, ag = goals.get('home'),goals.get('away')
                if not isinstance(hg,int) or not isinstance(ag,int):
                    continue
                if kind == 'goals':
                    hit = ((hg+ag)>threshold) == bool(positive)
                elif kind == 'btts':
                    hit = ((hg>0 and ag>0) == bool(positive))
                else:
                    continue
                result = 'won' if hit else 'lost'
            else:
                continue
            con.execute('UPDATE value_picks SET status=?,home_goals=?,away_goals=? WHERE id=?',
                        (result,hg,ag,id_))
            changed += 1
    return changed


async def publish_value_results(app):
    if not CHANNEL_ID:
        return
    with singles_db() as con:
        rows = con.execute('''SELECT id,status,odd,home_goals,away_goals
            FROM value_picks WHERE published_at IS NOT NULL
            AND result_posted_at IS NULL AND status IN ('won','lost','void') ORDER BY id''').fetchall()
    for id_,status,odd,hg,ag in rows:
        pnl = odd-1 if status=='won' else (-1 if status=='lost' else 0)
        msg = (f'🏁 OddX2 | PICK #{id_} RESULT\n{status.upper()}\n'
               f'Score: {hg if hg is not None else "-"}-{ag if ag is not None else "-"}\n'
               f'Quoted odds: {odd:.2f}\nHypothetical P/L: {pnl:+.2f} units\n'
               '1-unit hypothetical tracking; no real wagers.')
        await app.bot.send_message(chat_id=CHANNEL_ID,text=msg)
        with singles_db() as con:
            con.execute('UPDATE value_picks SET result_posted_at=? WHERE id=? AND result_posted_at IS NULL',
                        (datetime.now(timezone.utc).isoformat(),id_))


def value_report(period='all'):
    now = datetime.now(LOCAL)
    if period=='week':
        end = (now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        start = end-timedelta(days=7)
        label = f'{start:%d %b} - {(end-timedelta(days=1)):%d %b %Y}'
    elif period=='month':
        end = now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        start = (end-timedelta(days=1)).replace(day=1)
        label = f'{start:%B %Y}'
    elif period=='today':
        start = now.replace(hour=0,minute=0,second=0,microsecond=0)
        end = start+timedelta(days=1)
        label = f'{start:%d %b %Y}'
    else:
        start=end=None
        label='All time'
    with singles_db() as con:
        rows=con.execute('''SELECT published_at,status,odd FROM value_picks
            WHERE published_at IS NOT NULL''').fetchall()
    if start:
        rows=[r for r in rows if start <= datetime.fromisoformat(r[0]).astimezone(LOCAL) < end]
    wins=sum(r[1]=='won' for r in rows)
    losses=sum(r[1]=='lost' for r in rows)
    voids=sum(r[1]=='void' for r in rows)
    pending=sum(r[1]=='pending' for r in rows)
    settled=wins+losses
    pnl=sum((odd-1 if state=='won' else -1) for _,state,odd in rows if state in ('won','lost'))
    return (f'📈 OddX2 | INDIVIDUAL PICKS PERFORMANCE\nPeriod: {label}\n'
            f'Published: {len(rows)} | Won: {wins} | Lost: {losses} | '
            f'Void: {voids} | Pending: {pending}\n'
            f'Settled stake: {settled} units (voids excluded)\n'
            f'Hypothetical P/L: {pnl:+.2f} units\n'
            f'ROI: {pnl/settled:+.1%}' if settled else
            f'📈 OddX2 | INDIVIDUAL PICKS PERFORMANCE\nPeriod: {label}\n'
            f'Published: {len(rows)} | Won: {wins} | Lost: {losses} | '
            f'Void: {voids} | Pending: {pending}\nROI: N/A') + (
            '\nPublished picks only; fixed hypothetical 1-unit stakes. '
            'No actual bets. Legacy doubles excluded. Historical periods update as picks settle.')




# OddX2 5.4: diagnostic-only calibration. Same 5.3 cohort and publication rules.
# All reported ROI uses actual published selections, one hypothetical unit each.
def v54_diagnostic_rows():
    with v53_db() as con:
        return con.execute("""SELECT id,published_at,kickoff,league,bookmaker,market,
            selection,market_kind,positive,threshold,odd,estimated,implied,
            estimated_ev,home_n,away_n,status,home_goals,away_goals,created_at
            FROM direct_picks_53 WHERE published_at IS NOT NULL ORDER BY id""").fetchall()


def v54_summary(rows, name):
    w=sum(r[16]=='won' for r in rows)
    l=sum(r[16]=='lost' for r in rows)
    v=sum(r[16]=='void' for r in rows)
    p=sum(r[16]=='pending' for r in rows)
    n=w+l
    profit=sum((r[10]-1 if r[16]=='won' else -1) for r in rows if r[16] in ('won','lost'))
    avg_odd=sum(r[10] for r in rows if r[16] in ('won','lost'))/n if n else 0
    roi=f'{profit/n:+.1%}' if n else 'N/A'
    hit=f'{w/n:.1%}' if n else 'N/A'
    return f'{name}: {len(rows)} picks | {w}W {l}L {v}V {p}P | hit {hit} | avg odds {avg_odd:.2f} | P/L {profit:+.2f}u | ROI {roi}'


def v54_market_report():
    rows=v54_diagnostic_rows()
    def category(r):
        if r[7]=='btts':
            return 'BTTS Yes' if r[8] else 'BTTS No'
        if r[7]=='goals':
            direction='Over' if r[8] else 'Under'
            return f'{direction} {r[9]:g}' if r[9] is not None else direction
        return str(r[5])
    groups={}
    for r in rows:
        groups.setdefault(category(r),[]).append(r)
    lines=['📊 OddX2 5.4 | MARKET PERFORMANCE',
           'Same historical 5.3 published cohort; no reset.',
           v54_summary(rows,'ALL MARKETS'),'— By selection —']
    for name,group in sorted(groups.items(),key=lambda x:(-len(x[1]),x[0])):
        lines.append(v54_summary(group,name))
    lines.extend(['— By market family —'])
    for kind,label in [('goals','Total goals'),('btts','Both teams to score')]:
        lines.append(v54_summary([r for r in rows if r[7]==kind],label))
    lines.append('Small samples are noisy. Model EV remains uncalibrated. ROI is hypothetical, not guaranteed.')
    return '\n'.join(lines)


def v54_quality_report():
    rows=v54_diagnostic_rows()
    lines=['🔎 OddX2 5.4 | QUALITY & CALIBRATION',
           'Based only on selections published before kickoff.',
           v54_summary(rows,'ALL')]
    groups=[('Sample min 5–6',[r for r in rows if min(r[14],r[15])<=6]),
            ('Sample min 7–8',[r for r in rows if 7<=min(r[14],r[15])<=8]),
            ('Sample min 9+',[r for r in rows if min(r[14],r[15])>=9]),
            ('Model EV 5–14.9%',[r for r in rows if .05<=r[13]<.15]),
            ('Model EV 15–29.9%',[r for r in rows if .15<=r[13]<.30]),
            ('Model EV 30%+',[r for r in rows if r[13]>=.30])]
    for label,group in groups:
        lines.append(v54_summary(group,label))
    settled=[r for r in rows if r[16] in ('won','lost')]
    if settled:
        predicted=sum(r[11] for r in settled)/len(settled)
        actual=sum(r[16]=='won' for r in settled)/len(settled)
        brier=sum((r[11]-(1 if r[16]=='won' else 0))**2 for r in settled)/len(settled)
        lines.extend([f'Calibration on settled: model mean {predicted:.1%} vs actual {actual:.1%}',
                      f'Brier score: {brier:.3f} (lower is better; not a validated backtest).'])
    lines.append('Warning: cohort is selected by model EV, not an independent validation sample.')
    return '\n'.join(lines)


def v54_league_report():
    rows=v54_diagnostic_rows()
    groups={}
    for r in rows:
        groups.setdefault(r[3],[]).append(r)
    ranked=sorted(groups.items(),key=lambda item:(-sum(r[16] in ('won','lost') for r in item[1]),-len(item[1])))
    lines=['🌍 OddX2 5.4 | LEAGUE PERFORMANCE',v54_summary(rows,'ALL'),
           'Top 20 leagues by number of settled picks:']
    for name,group in ranked[:20]:
        lines.append(v54_summary(group,str(name)[:45]))
    lines.append('League comparisons are descriptive; no automatic exclusion based on tiny samples.')
    return '\n'.join(lines)


async def v54_reply_long(update, text):
    # Split only on line boundaries to keep Telegram readable.
    part=''
    for line in text.split('\n'):
        if len(part)+len(line)+1>3700:
            await update.message.reply_text(part)
            part=''
        part+=line+'\n'
    if part:
        await update.message.reply_text(part)


async def v54_market_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await v54_reply_long(update,await asyncio.to_thread(v54_market_report))


async def v54_quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await v54_reply_long(update,await asyncio.to_thread(v54_quality_report))


async def v54_league_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await v54_reply_long(update,await asyncio.to_thread(v54_league_report))


async def value_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 OddX2 5.1 | Scanning individual football value picks...')
    status = await asyncio.to_thread(value_scan)
    await publish_value_picks(context.application)
    await update.message.reply_text(f'Individual scan: {status}. See /picks and /report.')


async def value_picks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with singles_db() as con:
        rows=con.execute('''SELECT id,home,away,selection,odd,status,estimated_ev
            FROM value_picks WHERE published_at IS NOT NULL ORDER BY id DESC LIMIT 12''').fetchall()
    if not rows:
        await update.message.reply_text('No published individual picks yet.')
        return
    await update.message.reply_text('📋 OddX2 | RECENT INDIVIDUAL PICKS\n'+
        '\n'.join(f'#{id_} {home} vs {away} | {selection} @ {odd:.2f} | {status.upper()} | model EV {ev:+.1%}'
                  for id_,home,away,selection,odd,status,ev in rows))


async def value_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(value_report,'all'))


async def value_weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(value_report,'week'))


async def value_monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(value_report,'month'))


async def value_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(value_report,'today'))


async def legacy_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('OddX2 5.0 publishes individual picks only. Historical doubles are retained but no new doubles are generated. Use /scan, /picks or /report.')

def save_double(best):
    x, y, total = best
    # Repeated /analiza with the same selections does not create a duplicate.
    fingerprint = '|'.join(sorted(
        f"{c['fixture']}:{c['market_kind']}:{c['selection']}:{c['bookmaker']}"
        for c in (x, y)
    ))
    with db_connect() as con:
        # Do not reuse fixtures already included in ANY recorded double.
        existing = con.execute(
            'SELECT 1 FROM legs WHERE fixture_id IN (?,?) LIMIT 1',
            (x['fixture'], y['fixture'])
        ).fetchone()
        if existing:
            return False
        cur = con.execute(
            'INSERT OR IGNORE INTO doubles(created_at,fingerprint,bookmaker,combined_odds) VALUES(?,?,?,?)',
            (datetime.now(timezone.utc).isoformat(), fingerprint, x['bookmaker'], total),
        )
        if cur.rowcount == 0:
            return False
        double_id = cur.lastrowid
        for c in (x, y):
            con.execute('''INSERT INTO legs(
                double_id,fixture_id,kickoff,home,away,market,selection,
                market_kind,positive,threshold,odd,estimated
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (
                double_id, c['fixture'], c['kick'].isoformat(), c['home'], c['away'],
                c['market'], c['selection'], c['market_kind'], int(c['positive']),
                c['threshold'], c['odd'], c['estimated'],
            ))
    return True


def get_unpublished():
    with db_connect() as con:
        rows = con.execute("""SELECT id,bookmaker,combined_odds,created_at FROM doubles
                              WHERE published_at IS NULL ORDER BY id""").fetchall()
        output = []
        now = datetime.now(timezone.utc)
        for id_, book, total, created in rows:
            legs = con.execute("""SELECT kickoff,home,away,market,selection,odd,estimated
                                  FROM legs WHERE double_id=? ORDER BY id""", (id_,)).fetchall()
            # Skip historic doubles and games already underway. No retroactive picks.
            if (len(legs) != 2 or (now - datetime.fromisoformat(created).astimezone(timezone.utc)).total_seconds() > 900
                    or any(datetime.fromisoformat(l[0]).astimezone(timezone.utc) <= now + timedelta(minutes=MIN_LEAD_MINUTES) for l in legs)):
                continue
            output.append((id_, book, total, legs))
        return output


def get_unposted_results():
    with db_connect() as con:
        return con.execute("""SELECT id,status,combined_odds FROM doubles
                              WHERE published_at IS NOT NULL AND result_posted_at IS NULL
                              AND status IN ('won','lost') ORDER BY id""").fetchall()


def mark_posted(id_, field):
    if field not in ('published_at', 'result_posted_at'):
        raise ValueError('Invalid publication column')
    with db_connect() as con:
        con.execute(f'UPDATE doubles SET {field}=? WHERE id=? AND {field} IS NULL',
                    (datetime.now(timezone.utc).isoformat(), id_))


async def publish_updates(app):
    if not CHANNEL_ID:
        log.warning('Missing TELEGRAM_CHANNEL_ID; channel publishing disabled')
        return
    for id_, book, total, legs in await asyncio.to_thread(get_unpublished):
        lines = [f'📊 OddX2 | DOUBLE #{id_}', 'Experimental pre-match research', '']
        for i, (kick, home, away, market, selection, odd, estimated) in enumerate(legs, 1):
            kickoff = datetime.fromisoformat(kick).astimezone(LOCAL)
            lines.extend([f'{i}. {home} vs {away}',
                          f'Kickoff: {kickoff:%d %b %Y, %H:%M} (Bucharest)',
                          f'{market}: {selection} @ {odd:.2f}',
                          f'Experimental model estimate: {estimated:.1%}', ''])
        lines.extend([f'Bookmaker: {book}', f'Combined odds: {total:.2f}',
                      '⚠️ Uncalibrated estimates. Odds may change. No guaranteed returns.'])
        await app.bot.send_message(chat_id=CHANNEL_ID, text='\n'.join(lines))
        await asyncio.to_thread(mark_posted, id_, 'published_at')
        log.info('Published double #%s', id_)
    for id_, state, odds in await asyncio.to_thread(get_unposted_results):
        message = (f'🏁 OddX2 | DOUBLE #{id_} RESULT\n'
                   f'Outcome: {state.upper()}\nCombined odds: {odds:.2f}\n'
                   'Hypothetical tracking only; no actual bets placed.')
        await app.bot.send_message(chat_id=CHANNEL_ID, text=message)
        await asyncio.to_thread(mark_posted, id_, 'result_posted_at')
        log.info('Published result #%s', id_)


def daily_summary():
    with db_connect() as con:
        rows = con.execute("""SELECT status,combined_odds FROM doubles
                              WHERE published_at IS NOT NULL""").fetchall()
    wins = sum(state == 'won' for state, _ in rows)
    losses = sum(state == 'lost' for state, _ in rows)
    pending = sum(state == 'pending' for state, _ in rows)
    settled = wins + losses
    pnl = sum((odd - 1 if state == 'won' else -1)
              for state, odd in rows if state in ('won', 'lost'))
    return (f'📈 OddX2 | TRACK RECORD\nPublished doubles: {len(rows)}\n'
            f'Won: {wins} | Lost: {losses} | Pending: {pending}\n'
            + (f'Theoretical P/L: {pnl:+.2f} units | ROI: {pnl / settled:+.1%}'
               if settled else 'ROI: not available yet')
            + '\nBased on hypothetical 1-unit stakes. Not real betting returns.')


async def publish_results_and_summary(app):
    if not CHANNEL_ID:
        return
    outstanding = await asyncio.to_thread(get_unposted_results)
    if outstanding:
        await publish_updates(app)
        log.info('Results published; next public summary is weekly/monthly')


def settle_pending():
    """Only settle FT games; no guessing for AET/PEN or unavailable fixtures."""
    with db_connect() as con:
        rows = con.execute(
            "SELECT id,fixture_id,market_kind,positive,threshold FROM legs WHERE status='pending'"
        ).fetchall()
        fixture_cache = {}
        updated = 0
        for leg_id, fixture_id, kind, positive, threshold in rows:
            if fixture_id not in fixture_cache:
                try:
                    games = api('fixtures', {'id': fixture_id})
                    fixture_cache[fixture_id] = games[0] if games else None
                except (requests.RequestException, ValueError) as exc:
                    log.warning('Settlement lookup failed for %s: %s', fixture_id, exc)
                    fixture_cache[fixture_id] = None
            game = fixture_cache[fixture_id]
            if not game or game.get('fixture', {}).get('status', {}).get('short') != 'FT':
                continue
            goals = game.get('goals', {})
            hg, ag = goals.get('home'), goals.get('away')
            if hg is None or ag is None:
                continue
            if kind == 'goals':
                hit = ((hg + ag) > threshold) == bool(positive)
            elif kind == 'btts':
                hit = ((hg > 0 and ag > 0) == bool(positive))
            else:
                continue
            con.execute(
                'UPDATE legs SET status=?,home_goals=?,away_goals=? WHERE id=?',
                ('won' if hit else 'lost', hg, ag, leg_id),
            )
            updated += 1
        ids = [r[0] for r in con.execute("SELECT id FROM doubles WHERE status='pending'")]
        for double_id in ids:
            statuses = [r[0] for r in con.execute(
                'SELECT status FROM legs WHERE double_id=?', (double_id,)
            )]
            if 'lost' in statuses:
                state = 'lost'
            elif statuses and all(x == 'won' for x in statuses):
                state = 'won'
            else:
                continue
            con.execute('UPDATE doubles SET status=? WHERE id=?', (state, double_id))
        con.commit()
    return updated


async def analiza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 OddX2 5.0 — Running statistical analysis...')
    try:
        best, checked, qualified, count = await asyncio.to_thread(analyze)
        if not best:
            await update.message.reply_text(
                f'📊 Checked: {checked}; sufficient data: {qualified}; candidates: {count}. '
                'No qualifying double near 2.00. No picks forced.'
            )
            return
        saved = await asyncio.to_thread(save_double, best)
        x, y, total = best
        lines = ['🧪 OddX2 5.0 — Poisson model', 'Experimental statistical analysis', '']
        for i, c in enumerate((x, y), 1):
            lines.extend([
                f"{i}. {c['home']} vs {c['away']} — {c['kick'].astimezone(LOCAL):%H:%M}",
                f"{c['market']}: {c['selection']} @ {c['odd']:.2f}",
                f"Bookmaker: {c['bookmaker']}",
                f"Model estimate: {c['estimated']:.1%}; raw implied: {c['implied']:.1%}",
                f"Difference: {c['difference']:+.1%}; expected goals: {c['home_lambda']:.2f} - {c['away_lambda']:.2f}",
                f"Sample: {c['home_n']} home / {c['away_n']} away", '',
            ])
        lines.extend([
            f'🎯 Combined odds: {total:.2f}',
            '💾 Saved to history.' if saved else '💾 Already saved (duplicate selection).',
            '⚠️ Experimental and uncalibrated. Not proven betting value.',
        ])
        if saved:
            await publish_updates(context.application)
        await update.message.reply_text('\n'.join(lines))
    except Exception:
        log.exception('Poisson analysis failed')
        await update.message.reply_text('❌ Analysis error. Check Railway logs.')


async def istoric(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        def read():
            with db_connect() as con:
                return con.execute(
                    'SELECT id,created_at,combined_odds,status FROM doubles ORDER BY id DESC LIMIT 10'
                ).fetchall()
        rows = await asyncio.to_thread(read)
        if not rows:
            await update.message.reply_text('📚 History empty. Run /analiza to record the first double.')
            return
        lines = ['📚 Last 10 experimental doubles:']
        for id_, created, odds, status_ in rows:
            date = datetime.fromisoformat(created).astimezone(LOCAL)
            lines.append(f'#{id_} | {date:%d.%m %H:%M} | {odds:.2f} | {status_}')
        await update.message.reply_text('\n'.join(lines))
    except Exception:
        log.exception('History failed')
        await update.message.reply_text('❌ History error. Check Railway logs.')


async def statistici(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(settle_pending)
        def read():
            with db_connect() as con:
                return con.execute('SELECT status,combined_odds FROM doubles').fetchall()
        rows = await asyncio.to_thread(read)
        wins = sum(status_ == 'won' for status_, _ in rows)
        losses = sum(status_ == 'lost' for status_, _ in rows)
        pending = sum(status_ == 'pending' for status_, _ in rows)
        settled = wins + losses
        profit = sum((odds - 1) if status_ == 'won' else -1
                     for status_, odds in rows if status_ != 'pending')
        roi = 100 * profit / settled if settled else 0
        await update.message.reply_text(
            f'📊 Experimental history\nTotal: {len(rows)}\nWon: {wins}\nLost: {losses}'
            f'\nPending: {pending}\nSettled: {settled}\n'
            + (f'Theoretical P/L: {profit:+.2f} units\nROI: {roi:+.1f}%'
               if settled else 'ROI: not available (no settled doubles).')
            + '\n⚠️ Fixed 1-unit hypothetical stakes, not actual bets. No proof of model quality.'
        )
    except Exception:
        log.exception('Stats failed')
        await update.message.reply_text('❌ Stats error. Check Railway logs.')


async def verifica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        updated = await asyncio.to_thread(settle_pending)
        await publish_results_and_summary(context.application)
        await update.message.reply_text(f'🔄 Updated {updated} settled selections. Use /stats.')
    except Exception:
        log.exception('Settlement failed')
        await update.message.reply_text('❌ Settlement error. Check Railway logs.')


def automated_scan():
    """Find and store several non-overlapping qualifying doubles per scan."""
    now = datetime.now(timezone.utc)
    local_day = now.astimezone(LOCAL).date()
    with db_connect() as con:
        rows = con.execute('SELECT scanned_at FROM scan_runs ORDER BY id DESC LIMIT 100').fetchall()
        today_saved = con.execute('SELECT created_at FROM doubles').fetchall()
    today_count = sum(datetime.fromisoformat(t).astimezone(LOCAL).date() == local_day for (t,) in rows)
    saved_today = sum(datetime.fromisoformat(t).astimezone(LOCAL).date() == local_day for (t,) in today_saved)
    if today_count >= SCAN_LIMIT_PER_DAY:
        return 'daily_scan_limit_reached'
    remaining = max(0, DAILY_DOUBLE_TARGET - saved_today)
    if remaining == 0:
        return 'daily_double_target_reached'
    try:
        doubles, checked, qualified, candidates = analyze_batch(
            min(MAX_NEW_DOUBLES_PER_SCAN, remaining))
        saved_count = sum(save_double(double) for double in doubles)
        state = f'saved_{saved_count}' if saved_count else 'no_new_qualifying_double'
        detail = (f'proposed={len(doubles)} saved={saved_count}; '
                  f'min_lead={MIN_LEAD_MINUTES}m; football research only')
    except Exception as exc:
        log.exception('Automated scan failed')
        state, checked, qualified, candidates = 'error', 0, 0, 0
        detail = f'{type(exc).__name__}: {str(exc)[:160]}'
    with db_connect() as con:
        con.execute('INSERT INTO scan_runs(scanned_at,status,checked,qualified,candidates,detail) VALUES(?,?,?,?,?,?)',
                    (now.isoformat(), state, checked, qualified, candidates, detail))
    return state


async def scanare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 Running one research scan...')
    try:
        state = await asyncio.to_thread(automated_scan)
        await publish_updates(context.application)
        await update.message.reply_text(f'🧪 Scan: {state}. Check OddX2 channel, /history and /report.')
    except Exception:
        log.exception('Manual scan failed')
        await update.message.reply_text('❌ Scan failed; check Railway logs.')


async def raport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        def read():
            with db_connect() as con:
                doubles = con.execute('SELECT created_at,status,combined_odds FROM doubles').fetchall()
                scans = con.execute('SELECT scanned_at,status FROM scan_runs ORDER BY id DESC LIMIT 200').fetchall()
            return doubles, scans
        doubles, scans = await asyncio.to_thread(read)
        today = datetime.now(LOCAL).date()
        ds = [(s,o) for t,s,o in doubles if datetime.fromisoformat(t).astimezone(LOCAL).date() == today]
        ss = [s for t,s in scans if datetime.fromisoformat(t).astimezone(LOCAL).date() == today]
        wins = sum(s == 'won' for s,_ in ds)
        losses = sum(s == 'lost' for s,_ in ds)
        pending = sum(s == 'pending' for s,_ in ds)
        pnl = sum(o-1 if s == 'won' else -1 for s,o in ds if s in ('won','lost'))
        settled = wins + losses
        await update.message.reply_text(
            f'📊 Research report {today:%d.%m.%Y}\n'
            f'Scans: {len(ss)} | saved: {ss.count("saved")}\n'
            f'Doubles created today: {len(ds)}\nWon: {wins} | Lost: {losses} | Pending: {pending}\n'
            + (f'Theoretical P/L: {pnl:+.2f} units | ROI: {pnl/settled:+.1%}' if settled else 'ROI: not yet available for today')
            + '\n⚠️ Small samples are not evidence of predictive advantage.'
        )
    except Exception:
        log.exception('Report failed')
        await update.message.reply_text('❌ Report error; check Railway logs.')



SPORTS = {
    'basketball': ('https://v1.basketball.api-sports.io', 'API_BASKETBALL_KEY'),
    'hockey': ('https://v1.hockey.api-sports.io', 'API_HOCKEY_KEY'),
}


def multisport_api(sport, endpoint, params=None):
    base, env = SPORTS[sport]
    key = os.getenv(env, '').strip()
    if not key:
        raise ValueError(f'Missing {env}')
    response = requests.get(f'{base}/{endpoint}',
                            headers={'x-apisports-key': key},
                            params=params or {}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get('errors'):
        raise ValueError(str(data['errors']))
    return data.get('response', [])


def observe_sport(sport):
    # Data discovery only. These APIs do not share the football market schema.
    # No selection is published until a verified market and settlement adapter exists.
    endpoint = 'games'
    today = datetime.now(timezone.utc).date()
    counts, sample, state = 0, '', 'ok'
    try:
        for offset in range(2):
            games = multisport_api(sport, endpoint, {'date': (today + timedelta(days=offset)).isoformat()})
            counts += len(games)
            if games and not sample:
                game = games[0]
                # Store keys only: avoid logging private credentials or giant payloads.
                sample = ','.join(sorted(game.keys()))[:180]
    except Exception as exc:
        state = f'{type(exc).__name__}: {str(exc)[:100]}'
    with db_connect() as con:
        con.execute('INSERT INTO sport_observations(observed_at,sport,endpoint,item_count,sample,status) VALUES(?,?,?,?,?,?)',
                    (datetime.now(timezone.utc).isoformat(), sport, endpoint, counts, sample, state))
    return sport, counts, state


def multi_observations():
    return [observe_sport(sport) for sport in SPORTS]


async def sports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 Checking Basketball and Hockey feeds...')
    results = await asyncio.to_thread(multi_observations)
    lines = ['🏟 OddX2 | MULTISPORT FEEDS', 'Football: active statistical selections']
    for sport, count, state in results:
        lines.append(f'{sport.title()}: {count} games across today/tomorrow | {state}')
    lines.append('Basketball/Hockey: research previews available via /basketball and /hockey; automatic publishing disabled pending validation.')
    await update.message.reply_text('\n'.join(lines)[:4000])


def performance_report(period):
    now = datetime.now(LOCAL)
    if period == 'week':
        end = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        label = f'{start:%d %b} - {(end - timedelta(days=1)):%d %b %Y}'
    elif period == 'month':
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(day=1)
        label = f'{start:%B %Y}'
    else:
        start = end = None
        label = 'All time'
    def in_period(timestamp):
        if start is None:
            return True
        d = datetime.fromisoformat(timestamp).astimezone(LOCAL)
        return start <= d < end
    with db_connect() as con:
        rows = con.execute('SELECT published_at,status,combined_odds FROM doubles WHERE published_at IS NOT NULL').fetchall()
        scan_rows = con.execute('SELECT scanned_at,status,checked,qualified,candidates FROM scan_runs').fetchall()
    selected = [(status,odd) for published,status,odd in rows if in_period(published)]
    wins = sum(status == 'won' for status, _ in selected)
    losses = sum(status == 'lost' for status, _ in selected)
    pending = sum(status == 'pending' for status, _ in selected)
    settled = wins + losses
    pnl = sum((odd-1 if status == 'won' else -1) for status,odd in selected if status in ('won','lost'))
    scans = [r for r in scan_rows if in_period(r[0])]
    statuses = {}
    for _,state,*_ in scans:
        statuses[state] = statuses.get(state, 0) + 1
    avg = sum(odd for _,odd in selected)/len(selected) if selected else 0
    return (f'📈 OddX2 | {period.upper()} PERFORMANCE\n'
            f'Period: {label}\n'
            f'Published doubles: {len(selected)}\n'
            f'Won: {wins} | Lost: {losses} | Pending: {pending}\n'
            f'Win rate: {wins/settled:.1%}\n' if settled else
            f'📈 OddX2 | {period.upper()} PERFORMANCE\n'
            f'Period: {label}\n'
            f'Published doubles: {len(selected)}\n'
            f'Won: {wins} | Lost: {losses} | Pending: {pending}\n'
            f'Win rate: N/A\n') + (
            f'Average combined odds: {avg:.2f}\n'
            f'Theoretical P/L: {pnl:+.2f} units\n'
            f'ROI: {pnl/settled:+.1%}\n' if settled else
            f'Average combined odds: {avg:.2f}\nROI: N/A\n') + (
            f'Scans: {len(scans)}\n'
            f'Scan outcomes: {statuses}\n'
            'Published-pick cohort; fixed hypothetical 1-unit stakes. '
            'Later settlements can update historical period results. '
            'No proven predictive advantage.')


async def weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(performance_report, 'week'))


async def monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(performance_report, 'month'))


async def alltime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(performance_report, 'all'))


async def diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def read():
        with db_connect() as con:
            return con.execute('SELECT scanned_at,status,checked,qualified,candidates FROM scan_runs ORDER BY id DESC LIMIT 12').fetchall()
    rows = await asyncio.to_thread(read)
    lines = ['🔎 OddX2 | LAST 12 SCANS']
    for stamp,state,checked,qualified,candidates in rows:
        d = datetime.fromisoformat(stamp).astimezone(LOCAL)
        lines.append(f'{d:%d %b %H:%M} | {state} | checked {checked}, sufficient data {qualified}, candidates {candidates}')
    await update.message.reply_text('\n'.join(lines)[:4000])


async def footballaudit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 OddX2 5.0 | Auditing football coverage (read-only)...')
    try:
        audit = {}
        selected, inspected, qualified, candidates = await asyncio.to_thread(analyze_batch, 1, audit)
        lines = [
            '⚽ OddX2 5.0 | FOOTBALL COVERAGE AUDIT',
            f'20h fixtures: {audit["total"]} | analysis budget: {FOOTBALL_SCAN_BUDGET} | selected: {audit["selected"]}',
            f'Equal-priority UEFA/Top 5 reserve: {FOOTBALL_PRIORITY_RESERVE}; remaining slots for other competitions',
            f'Checked: {inspected} | enough history: {qualified} | eligible quotes: {candidates}',
            'Competition | available / selected / checked / history / odds / edge outcomes',
        ]
        entries = sorted(audit['leagues'].items(),
                         key=lambda item: (-item[1]['selected'], -item[1]['available'], item[0]))
        for name, row in entries[:35]:
            lines.append(f'{name}: {row["available"]}/{row["selected"]}/{row["checked"]}/'
                         f'{row["history"]}/{row["odds"]}/{row["edge"]}')
        lines.append('Edge outcomes are quotes, not independent games. '
                     'Research only; no records saved or channel posts created.')
        message = '\n'.join(lines)
        for i in range(0, len(message), 3900):
            await update.message.reply_text(message[i:i+3900])
    except Exception:
        log.exception('Football audit failed')
        await update.message.reply_text('❌ Football audit failed. Check Railway logs.')


async def pairaudit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read-only audit: uses the same model and constraints as the live scan."""
    await update.message.reply_text('🔎 OddX2 5.0 | Auditing football pair construction (read-only)...')
    try:
        audit = {}
        _, checked, history, remaining = await asyncio.to_thread(analyze_batch, 3, audit)
        f = audit.get('pair_filters', {})
        q = audit.get('data_quality', {})
        lines = [
            '⚽ OddX2 5.0 | FOOTBALL PAIR AUDIT',
            'Read-only. No records saved or channel posts created.',
            f'Fixtures: {audit.get("total", 0)} available | {audit.get("selected", 0)} selected '
            f'| {checked} inspected | {history} with sufficient history',
            f'Quotes: {audit.get("unique_quotes", 0)} unique after ranking cap (150) '
            f'| {audit.get("reused_quotes", 0)} excluded due to historical reuse '
            f'| {remaining} remaining',
            f'Distinct events: {audit.get("distinct_events", 0)} '
            f'| bookmakers: {audit.get("bookmakers", 0)}',
            'QUOTE SAMPLE QUALITY (not independent games):',
            f'5-6 home/away samples: {q.get("minimum_5_to_6", 0)}',
            f'7-9 home/away samples: {q.get("moderate_7_to_9", 0)}',
            f'10+ home/away samples: {q.get("strong_10_plus", 0)}',
            'PAIR FILTERS (sequential; counts are mutually exclusive):',
            f'Same fixture: {f.get("same_event", 0)}',
            f'Different bookmaker: {f.get("different_bookmaker", 0)}',
            f'Outside combined 1.90-2.10: {f.get("outside_target", 0)}',
            f'Qualifying combinations: {f.get("qualifying", 0)}',
            f'Independent doubles available in this scan: {audit.get("selected_pairs", 0)}',
        ]
        if audit.get('top_pairs'):
            lines.append('TOP PAIRS (not posted):')
            for pair in audit['top_pairs']:
                lines.append(
                    f'{pair["a"]} + {pair["b"]} | {pair["bookmaker"]} '
                    f'@ {pair["combined"]:.2f} | sum model gaps {pair["gap"]:+.1%} '
                    f'| samples {pair["samples"][0]}/{pair["samples"][1]}'
                )
        lines.append('Model gaps are uncalibrated; a positive gap is NOT proof of value. '
                     'Competitions are prioritized for coverage, not assigned bonus probabilities.')
        message = '\n\n'.join(lines)
        for i in range(0, len(message), 3800):
            await update.message.reply_text(message[i:i+3800])
    except Exception:
        log.exception('Football pair audit failed')
        await update.message.reply_text('❌ Pair audit error. Check Railway logs.')


async def scheduled_reports(app):
    while True:
        try:
            now = datetime.now(LOCAL)
            if CHANNEL_ID and now.hour >= 9:
                for kind, due in [('week', now.weekday() == 0), ('month', now.day == 1)]:
                    if not due:
                        continue
                    period_key = ((now - timedelta(days=7)).strftime('%G-W%V') if kind == 'week'
                                  else (now.replace(day=1) - timedelta(days=1)).strftime('%Y-%m'))
                    with db_connect() as con:
                        already = con.execute('SELECT 1 FROM report_posts WHERE period_type=? AND period_key=?',
                                              ('singles_'+kind,period_key)).fetchone()
                    if already:
                        continue
                    message = await asyncio.to_thread(value_report,kind)
                    await app.bot.send_message(chat_id=CHANNEL_ID, text=message)
                    with db_connect() as con:
                        con.execute('INSERT OR IGNORE INTO report_posts(period_type,period_key,sent_at) VALUES(?,?,?)',
                                    ('singles_'+kind,period_key,datetime.now(timezone.utc).isoformat()))
        except Exception:
            log.exception('Scheduled report failed')
        await asyncio.sleep(3600)


async def periodic_sport_observations(app):
    await asyncio.sleep(180)
    while True:
        try:
            await asyncio.to_thread(multi_observations)
        except Exception:
            log.exception('Multisport discovery failed')
        await asyncio.sleep(6*3600)


async def periodic_scanning(app):
    # Wait after startup so the first scan is not triggered by each deployment.
    await asyncio.sleep(300)
    while True:
        try:
            state = await asyncio.to_thread(value_scan)
            log.info('Automated research scan: %s', state)
            await publish_value_picks(app)
        except Exception:
            log.exception('Research scan failed')
        await asyncio.sleep(AUTO_SCAN_HOURS * 3600)


async def periodic_settlement(app):
    while True:
        await asyncio.sleep(1800)
        try:
            await asyncio.to_thread(settle_pending)  # preserve legacy ledger settlement
            await asyncio.to_thread(settle_value_picks)
            await publish_value_results(app)
        except Exception:
            log.exception('Background settlement failed')


async def testchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID or not update.effective_user or update.effective_chat.type != 'private':
        return
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, update.effective_user.id)
        if member.status not in ('creator', 'administrator'):
            await update.message.reply_text('Only channel administrators can run this test.')
            return
        await context.bot.send_message(chat_id=CHANNEL_ID, text='✅ OddX2 channel connection confirmed. Research only.')
        await update.message.reply_text('Test delivered to OddX2 channel.')
    except Exception:
        log.exception('Channel test failed')
        await update.message.reply_text('Channel test failed. Check Railway logs.')


async def on_startup(app):
    await asyncio.to_thread(db_connect_and_close)
    app.create_task(periodic_settlement(app))
    app.create_task(periodic_scanning(app))
    app.create_task(scheduled_reports(app))
    app.create_task(periodic_sport_observations(app))
    app.create_task(publish_value_picks(app))


def db_connect_and_close():
    con = singles_db()
    con.close()



# 3.2 research-only adapters. They do not write to SQLite or publish picks.
def ms_finished_score(sport, game):
    status = game.get('status', {}).get('short')
    if sport == 'basketball':
        if status != 'FT':
            return None
        scores = game.get('scores') or {}
        h, a = scores.get('home') or {}, scores.get('away') or {}
        if h.get('total') is None or a.get('total') is None:
            return None
        return int(h['total']), int(a['total'])
    if status not in ('FT', 'AOT', 'AP'):
        return None
    periods = game.get('periods') or {}
    try:
        pairs = [tuple(int(n) for n in periods[key].split('-')) for key in ('first', 'second', 'third')]
        if any(len(pair) != 2 for pair in pairs):
            return None
        return sum(x for x, _ in pairs), sum(y for _, y in pairs)
    except (AttributeError, ValueError, TypeError, KeyError):
        return None


def ms_history(sport, team_id, season, league_id, before, cache):
    key = (sport, team_id, str(season), league_id)
    if key not in cache:
        data = multisport_api(sport, 'games', {'team': team_id, 'season': season, 'league': league_id})
        records = []
        for g in data:
            try:
                when = datetime.fromisoformat(g['date'].replace('Z', '+00:00')).astimezone(timezone.utc)
                score = ms_finished_score(sport, g)
                if score is None:
                    continue
                home_id = (g.get('teams', {}).get('home') or {}).get('id')
                away_id = (g.get('teams', {}).get('away') or {}).get('id')
                if home_id == team_id:
                    records.append((when, 'home', score[0], score[1]))
                elif away_id == team_id:
                    records.append((when, 'away', score[1], score[0]))
            except (KeyError, ValueError, TypeError):
                continue
        cache[key] = sorted(records, reverse=True)
    return [r for r in cache[key] if r[0] < before]


def ms_preview(sport, limit=5):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=20)
    games = []
    for day in sorted({now.date().isoformat(), end.date().isoformat()}):
        games.extend(multisport_api(sport, 'games', {'date': day}))
    upcoming = []
    for g in games:
        try:
            when = datetime.fromisoformat(g['date'].replace('Z', '+00:00')).astimezone(timezone.utc)
            if now + timedelta(minutes=20) < when <= end:
                upcoming.append((when, g))
        except (KeyError, ValueError, TypeError):
            pass
    upcoming.sort(key=lambda t: t[0])
    cache, candidates, checked, qualified, priced = {}, [], 0, 0, 0
    for when, g in upcoming[:18]:
        league = g.get('league') or {}
        teams = g.get('teams') or {}
        h, a = teams.get('home') or {}, teams.get('away') or {}
        if not all((h.get('id'), a.get('id'), league.get('id'), league.get('season') is not None)):
            continue
        checked += 1
        try:
            home = [r for r in ms_history(sport, h['id'], league['season'], league['id'], when, cache) if r[1] == 'home'][:10]
            away = [r for r in ms_history(sport, a['id'], league['season'], league['id'], when, cache) if r[1] == 'away'][:10]
            if len(home) < 5 or len(away) < 5:
                continue
            qualified += 1
            expected_home = (sum(r[2] for r in home)/len(home) + sum(r[3] for r in away)/len(away))/2
            expected_away = (sum(r[2] for r in away)/len(away) + sum(r[3] for r in home)/len(home))/2
            odds = multisport_api(sport, 'odds', {'game': g['id']})
            if odds:
                priced += 1
            for entry in odds:
                for book in entry.get('bookmakers', []):
                    for bet in book.get('bets', []):
                        # Preview only: no assumed odds settlement semantics.
                        if bet.get('id') != 4:
                            continue
                        for v in bet.get('values', []):
                            label = str(v.get('value', ''))
                            match = re.fullmatch(r'(Over|Under)\s+(\d+\.5)', label, re.I)
                            if not match:
                                continue
                            try:
                                price = float(v['odd'])
                            except (KeyError, ValueError, TypeError):
                                continue
                            if not math.isfinite(price) or not 1.25 <= price <= 1.85:
                                continue
                            line = float(match.group(2))
                            gap = (expected_home + expected_away - line) * (1 if match.group(1).lower() == 'over' else -1)
                            candidates.append((gap, when, h.get('name','?'), a.get('name','?'), book.get('name','?'), label, price, expected_home + expected_away, len(home), len(away)))
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning('3.2 %s preview skipped game %s: %s', sport, g.get('id'), exc)
    candidates.sort(key=lambda r: r[0], reverse=True)
    # Not probabilities, value bets, or picks. Descriptive differences only.
    return checked, qualified, priced, candidates[:limit]


async def ms_command(update: Update, context: ContextTypes.DEFAULT_TYPE, sport):
    await update.message.reply_text(f'🔎 OddX2 3.2 | {sport.title()} research preview...')
    try:
        checked, qualified, priced, rows = await asyncio.to_thread(ms_preview, sport)
        lines = [f'🏟 OddX2 3.2 | {sport.upper()} PREVIEW',
                 f'Upcoming inspected: {checked} | enough history: {qualified} | with odds: {priced}',
                 'Research only — NOT predictions, betting value, or published selections.']
        for gap, when, home, away, book, label, price, estimate, hn, an in rows:
            lines.append(f'{when.astimezone(LOCAL):%d %b %H:%M} | {home} vs {away}\n'
                         f'{book}: {label} @ {price:.2f} | historical total estimate: {estimate:.1f} '
                         f'| difference: {gap:+.1f} points/goals | sample {hn}H/{an}A')
        if not rows:
            lines.append('No preview candidates with sufficient data and available odds.')
        if sport == 'hockey':
            lines.append('Hockey history uses regulation-time goals only. Odds O/U settlement rules remain unverified.')
        else:
            lines.append('Basketball total includes final scores; overtime settlement rules remain unverified.')
        await update.message.reply_text('\n\n'.join(lines)[:4000])
    except Exception:
        log.exception('3.2 %s research preview failed', sport)
        await update.message.reply_text('❌ Research preview unavailable. Check Railway logs.')


async def basketball_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ms_command(update, context, 'basketball')


async def hockey_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ms_command(update, context, 'hockey')


# OddX2 3.5-beta: read-only multisport probability research.
# No writes to SQLite, no Telegram channel posts, and no asserted bookmaker value.
def ms_probabilities(sport, home, away, expected_home, expected_away, market, label,
                     model='improved'):
    """Exploratory probabilities. Historical records must predate the fixture."""
    if model == 'legacy':
        return ms_probabilities_legacy(sport, home, away, expected_home,
                                       expected_away, market, label)
    if sport == 'basketball':
        if market != 4:
            return None
        match = re.fullmatch(r'(Over|Under)\s+(\d+\.5)', label, re.I)
        if not match:
            return None
        # Variance is estimated from two independent venue-specific histories.
        # Regularization reduces volatility from samples as small as five.
        ht = [r[2] + r[3] for r in home]
        at = [r[2] + r[3] for r in away]
        if len(ht) < 5 or len(at) < 5:
            return None
        def variance(values):
            mean = sum(values) / len(values)
            return sum((v - mean)**2 for v in values) / max(1, len(values)-1)
        empirical = (variance(ht) + variance(at)) / 2
        # Weak prior, not fitted to historical outcomes.
        sd = math.sqrt(0.70 * empirical + 0.30 * 22.0**2)
        sd = max(14.0, min(45.0, sd))
        z = (float(match.group(2)) - expected_home - expected_away) / sd
        under = 0.5 * math.erfc(-z / math.sqrt(2))
        prob = 1 - under if match.group(1).lower() == 'over' else under
        return max(0.001, min(0.999, prob)), 'full-game total; OT settlement unverified'
    if sport == 'hockey':
        if market != 1 or label not in ('Home', 'Draw', 'Away'):
            return None
        # Shrink unstable venue averages towards conservative neutral priors.
        # 3-way outcomes are explicitly settled at 60 minutes.
        n = min(len(home), len(away))
        weight = n / (n + 8.0)
        hlam = max(0.20, min(weight * expected_home + (1-weight)*2.85, 6.0))
        alam = max(0.20, min(weight * expected_away + (1-weight)*2.65, 6.0))
        hp = [math.exp(-hlam) * hlam**i / math.factorial(i) for i in range(26)]
        ap = [math.exp(-alam) * alam**i / math.factorial(i) for i in range(26)]
        probs = {'Home': 0.0, 'Draw': 0.0, 'Away': 0.0}
        for i, x in enumerate(hp):
            for j, y in enumerate(ap):
                probs['Home' if i > j else 'Away' if i < j else 'Draw'] += x*y
        norm = sum(probs.values())
        return probs[label] / norm, '60-minute 3-way; regularized Poisson'
    return None


def ms_probabilities_legacy(sport, home, away, expected_home, expected_away, market, label):
    """Frozen 3.5 comparator for paired, same-fixture backtests."""
    if sport == 'basketball':
        if market != 4:
            return None
        m = re.fullmatch(r'(Over|Under)\s+(\d+\.5)', label, re.I)
        if not m:
            return None
        totals = [r[2]+r[3] for r in home] + [r[2]+r[3] for r in away]
        if len(totals) < 10:
            return None
        avg = sum(totals)/len(totals)
        sd = math.sqrt(sum((v-avg)**2 for v in totals)/(len(totals)-1))
        sd = max(sd, 15.0)
        z = (float(m.group(2)) - (expected_home+expected_away))/sd
        under = 0.5*(1+math.erf(z/math.sqrt(2)))
        return (1-under if m.group(1).lower() == 'over' else under), 'legacy'
    if sport == 'hockey':
        if market != 1 or label not in ('Home','Draw','Away'):
            return None
        hlam = max(0.15,min(expected_home,7.0))
        alam = max(0.15,min(expected_away,7.0))
        hp = [math.exp(-hlam)*hlam**i/math.factorial(i) for i in range(20)]
        ap = [math.exp(-alam)*alam**i/math.factorial(i) for i in range(20)]
        probs = {'Home':0.0,'Draw':0.0,'Away':0.0}
        for i,x in enumerate(hp):
            for j,y in enumerate(ap):
                probs['Home' if i>j else 'Away' if i<j else 'Draw'] += x*y
        return probs[label], 'legacy'
    return None


def ms_collect_candidates(sport, now, horizon_hours=48, max_games=60):
    end = now + timedelta(hours=horizon_hours)
    games = []
    for offset in range((end.date() - now.date()).days + 1):
        day = (now.date() + timedelta(days=offset)).isoformat()
        games.extend(multisport_api(sport, 'games', {'date':day}))
    upcoming = []
    for g in games:
        try:
            when = datetime.fromisoformat(g['date'].replace('Z','+00:00')).astimezone(timezone.utc)
            if now + timedelta(minutes=20) < when <= end:
                upcoming.append((when,g))
        except (ValueError,KeyError,TypeError):
            continue
    upcoming.sort(key=lambda item:item[0])
    cache, candidates = {}, []
    stats = {'upcoming':len(upcoming),'inspected':0,'history':0,'priced':0,'errors':0,
             'market_outcomes':0,'supported_markets':0,'odds_rejected':0,
             'edge_rejected':0,'edge_passed':0,'unique_events_priced':set(), 'unique_events_passed':set()}
    for when,g in upcoming[:max_games]:
        league, teams = g.get('league') or {}, g.get('teams') or {}
        h, a = teams.get('home') or {}, teams.get('away') or {}
        if not all((g.get('id'),h.get('id'),a.get('id'),league.get('id'),league.get('season') is not None)):
            continue
        stats['inspected'] += 1
        try:
            home = [r for r in ms_history(sport,h['id'],league['season'],league['id'],when,cache) if r[1]=='home'][:10]
            away = [r for r in ms_history(sport,a['id'],league['season'],league['id'],when,cache) if r[1]=='away'][:10]
            if len(home)<5 or len(away)<5:
                continue
            stats['history'] += 1
            eh = (sum(r[2] for r in home)/len(home)+sum(r[3] for r in away)/len(away))/2
            ea = (sum(r[2] for r in away)/len(away)+sum(r[3] for r in home)/len(home))/2
            odds = multisport_api(sport,'odds',{'game':g['id']})
            if odds:
                stats['priced'] += 1
                stats['unique_events_priced'].add(g['id'])
            for entry in odds:
                for book in entry.get('bookmakers',[]):
                    for bet in book.get('bets',[]):
                        for v in bet.get('values',[]):
                            stats['market_outcomes'] += 1
                            label = str(v.get('value','')).strip()
                            result = ms_probabilities(sport,home,away,eh,ea,bet.get('id'),label)
                            if result is None: continue
                            stats['supported_markets'] += 1
                            try: odd = float(v.get('odd'))
                            except (ValueError,TypeError):
                                stats['odds_rejected'] += 1
                                continue
                            if not math.isfinite(odd) or not 1.25<=odd<=1.85:
                                stats['odds_rejected'] += 1
                                continue
                            prob, rule = result
                            edge = prob - 1/odd
                            if not math.isfinite(edge) or edge < 0.05:
                                stats['edge_rejected'] += 1
                                continue
                            stats['edge_passed'] += 1
                            stats['unique_events_passed'].add(g['id'])
                            candidates.append({'sport':sport,'fixture':g['id'],'kick':when,
                                'home':h.get('name','?'),'away':a.get('name','?'),
                                'bookmaker':book.get('name','?'),'market':bet.get('name','?'),
                                'selection':label,'odd':odd,'estimated':prob,'difference':edge,
                                'sample':f'{len(home)}H/{len(away)}A','rule':rule})
        except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
            stats['errors'] += 1
            log.warning('3.4 %s skipped game %s: %s',sport,g.get('id'),exc)
    stats['unique_events_priced'] = len(stats['unique_events_priced'])
    stats['unique_events_passed'] = len(stats['unique_events_passed'])
    return candidates, stats


def multiscan_preview():
    """Read-only diagnostic scan. Never alters saved doubles or channel posts."""
    now = datetime.now(timezone.utc)
    all_candidates, stats = [], {}
    for sport in ('basketball','hockey'):
        try:
            rows, stat = ms_collect_candidates(sport,now)
            all_candidates.extend(rows)
            stats[sport] = stat
        except Exception as exc:
            stats[sport] = {'error':f'{type(exc).__name__}: {str(exc)[:100]}'}
            log.exception('3.4 %s multiscan failed',sport)
    with db_connect() as con:
        columns = {r[1] for r in con.execute('PRAGMA table_info(legs)')}
        if 'sport' in columns:
            used = {(r[0] or 'football',r[1]) for r in con.execute('SELECT sport,fixture_id FROM legs')}
        else:
            used = {('football',r[0]) for r in con.execute('SELECT fixture_id FROM legs')}
    diag = {'raw_candidates':len(all_candidates),'historical_reuse':0,
            'duplicate_quotes':0,'unique_candidates':0,'pair_checks':0,
            'same_event':0,'different_bookmaker':0,'combined_odds_outside':0,
            'qualifying_pairs':0,'overlap_excluded':0}
    unique = {}
    for c in all_candidates:
        key = (c['sport'],c['fixture'],c['bookmaker'].casefold(),c['market'],c['selection'])
        if (c['sport'],c['fixture']) in used:
            diag['historical_reuse'] += 1
            continue
        if key in unique:
            diag['duplicate_quotes'] += 1
        if key not in unique or c['difference'] > unique[key]['difference']:
            unique[key] = c
    # Diversification: retain up to three distinct market lines per event and bookmaker.
    # This prevents one heavily quoted game from crowding out other events.
    per_event_book = {}
    for c in sorted(unique.values(), key=lambda c:c['difference'], reverse=True):
        group = (c['sport'], c['fixture'], c['bookmaker'].casefold())
        bucket = per_event_book.setdefault(group, [])
        if len(bucket) < 3:
            bucket.append(c)
    choices = [c for bucket in per_event_book.values() for c in bucket]
    choices.sort(key=lambda c:c['difference'], reverse=True)
    choices = choices[:300]
    diag['unique_candidates'] = len(choices)
    diag['distinct_events'] = len({(c['sport'],c['fixture']) for c in choices})
    diag['distinct_bookmakers'] = len({c['bookmaker'].casefold() for c in choices})
    diag['diversification_dropped'] = len(unique) - len(choices)
    pairs=[]
    for x,y in combinations(choices,2):
        diag['pair_checks'] += 1
        if (x['sport'],x['fixture'])==(y['sport'],y['fixture']):
            diag['same_event'] += 1
            continue
        if x['bookmaker'].casefold()!=y['bookmaker'].casefold():
            diag['different_bookmaker'] += 1
            continue
        total = x['odd']*y['odd']
        if not 1.90<=total<=2.10:
            diag['combined_odds_outside'] += 1
            continue
        diag['qualifying_pairs'] += 1
        pairs.append((x['difference']+y['difference'],x,y,total))
    pairs.sort(key=lambda r:r[0],reverse=True)
    selected, reserved = [],set()
    for _,x,y,total in pairs:
        keys = {(x['sport'],x['fixture']),(y['sport'],y['fixture'])}
        if keys & reserved:
            diag['overlap_excluded'] += 1
            continue
        selected.append((x,y,total))
        reserved |= keys
        if len(selected)==3: break
    return selected,stats,diag


async def multiscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 OddX2 3.6 | Running read-only multisport diagnostic scan (48h, up to 60 games per sport)...')
    try:
        pairs,stats,diag = await asyncio.to_thread(multiscan_preview)
        lines = ['🧪 OddX2 3.6-beta | MULTISPORT DIAGNOSTICS',
                 'Read-only. Nothing saved or published. Uncalibrated model.']
        for sport,st in stats.items():
            if 'error' in st:
                lines.append(f'{sport.title()}: ERROR {st["error"]}')
                continue
            lines.append(f'{sport.title()}: upcoming {st["upcoming"]}, inspected {st["inspected"]}, '
                         f'history {st["history"]}, priced {st["priced"]}, API errors {st["errors"]}\n'
                         f'Unique priced events {st["unique_events_priced"]}; events passing edge {st["unique_events_passed"]}\n'
                         f'Outcomes {st["market_outcomes"]} → supported {st["supported_markets"]} '
                         f'→ odds filtered {st["odds_rejected"]} '
                         f'→ edge filtered {st["edge_rejected"]} '
                         f'→ edge passed {st["edge_passed"]}')
        lines.append('PAIR FILTERS: '
                     f'raw {diag["raw_candidates"]}, historical reuse {diag["historical_reuse"]}, '
                     f'duplicate quotes {diag["duplicate_quotes"]}, diversified {diag["unique_candidates"]}, '
                     f'lines dropped {diag["diversification_dropped"]}\n'
                     f'Distinct events {diag["distinct_events"]}; bookmakers {diag["distinct_bookmakers"]}\n'
                     f'Pairs checked {diag["pair_checks"]}: same event {diag["same_event"]}, '
                     f'different bookmaker {diag["different_bookmaker"]}, '
                     f'outside 1.90–2.10 {diag["combined_odds_outside"]}, '
                     f'qualifying {diag["qualifying_pairs"]}.')
        for i,(x,y,total) in enumerate(pairs,1):
            lines.append(f'DOUBLE {i} | {x["bookmaker"]} | combined {total:.2f}')
            for c in (x,y):
                lines.append(f'{c["sport"].title()} | {c["kick"].astimezone(LOCAL):%d %b %H:%M} '
                             f'| {c["home"]} vs {c["away"]}\n'
                             f'{c["market"]}: {c["selection"]} @ {c["odd"]:.2f} '
                             f'| model {c["estimated"]:.1%} | gap {c["difference"]:+.1%} | {c["sample"]}')
        if not pairs: lines.append('No qualifying double. Nothing forced.')
        lines.append('⚠️ Research only. Basketball OT rules unverified; hockey 3Way is regulation. No proven advantage.')
        message = '\n\n'.join(lines)
        for i in range(0,len(message),3900):
            await update.message.reply_text(message[i:i+3900])
    except Exception:
        log.exception('3.4 multiscan failed')
        await update.message.reply_text('❌ Multisport scan failed. Check Railway logs.')


def ms_rolling_backtest(sport, days=45, max_tests=75):
    """Paired, read-only rolling evaluation; no historical bookmaker odds.

    Compares the same fixtures under 3.5, 3.6 and naive probabilities.
    Uses only completed games dated before each target fixture for features.
    """
    now = datetime.now(timezone.utc)
    cache, games, seen = {}, [], set()
    for day_back in range(1, days + 1):
        date = (now - timedelta(days=day_back)).date().isoformat()
        for game in multisport_api(sport, 'games', {'date': date}):
            if game.get('id') in seen or ms_finished_score(sport, game) is None:
                continue
            seen.add(game['id'])
            games.append(game)
    games.sort(key=lambda g: g.get('date', ''), reverse=True)
    result = {'tested': 0, 'eligible': 0, 'errors': 0, 'correct': 0,
              'old_correct': 0, 'new_brier': 0., 'old_brier': 0.,
              'baseline_brier': 0., 'sport': sport, 'leagues': {}, 'days': days, 'scanned': 0}
    for game in games[:450]:
        result['scanned'] += 1
        if result['tested'] >= max_tests:
            break
        try:
            league, teams = game.get('league') or {}, game.get('teams') or {}
            h, a = teams.get('home') or {}, teams.get('away') or {}
            if not all((h.get('id'), a.get('id'), league.get('id'),
                        league.get('season') is not None)):
                continue
            when = datetime.fromisoformat(game['date'].replace('Z', '+00:00')).astimezone(timezone.utc)
            home = [r for r in ms_history(sport, h['id'], league['season'],
                    league['id'], when, cache) if r[1] == 'home'][:10]
            away = [r for r in ms_history(sport, a['id'], league['season'],
                    league['id'], when, cache) if r[1] == 'away'][:10]
            if len(home) < 5 or len(away) < 5:
                continue
            eh = (sum(r[2] for r in home)/len(home) +
                  sum(r[3] for r in away)/len(away))/2
            ea = (sum(r[2] for r in away)/len(away) +
                  sum(r[3] for r in home)/len(home))/2
            hs, ass = ms_finished_score(sport, game)
            result['eligible'] += 1
            if sport == 'basketball':
                line = math.floor(eh + ea) + 0.5
                label = f'Over {line:.1f}'
                outcome = int(hs + ass > line)
                new = ms_probabilities(sport, home, away, eh, ea, 4, label)
                old = ms_probabilities(sport, home, away, eh, ea, 4, label, model='legacy')
                if new is None or old is None:
                    continue
                pn, po = new[0], old[0]
                result['new_brier'] += (pn-outcome)**2
                result['old_brier'] += (po-outcome)**2
                result['baseline_brier'] += (0.5-outcome)**2
                result['correct'] += int((pn >= 0.5) == bool(outcome))
                result['old_correct'] += int((po >= 0.5) == bool(outcome))
            else:
                outcome = 'Home' if hs > ass else 'Away' if hs < ass else 'Draw'
                labels = ('Home', 'Draw', 'Away')
                new = {k: ms_probabilities(sport, home, away, eh, ea, 1, k)[0]
                       for k in labels}
                old = {k: ms_probabilities(sport, home, away, eh, ea, 1, k,
                       model='legacy')[0] for k in labels}
                result['new_brier'] += sum((p-int(k == outcome))**2 for k, p in new.items())
                result['old_brier'] += sum((p-int(k == outcome))**2 for k, p in old.items())
                result['baseline_brier'] += 2/3
                result['correct'] += int(max(new, key=new.get) == outcome)
                result['old_correct'] += int(max(old, key=old.get) == outcome)
            result['tested'] += 1
            league_key = f'{league.get("name", "Unknown")} ({league.get("id")})'
            record = result['leagues'].setdefault(league_key, {'n': 0, 'new': 0., 'old': 0.})
            record['n'] += 1
            if sport == 'basketball':
                record['new'] += (pn-outcome)**2
                record['old'] += (po-outcome)**2
            else:
                record['new'] += sum((v-int(k == outcome))**2 for k, v in new.items())
                record['old'] += sum((v-int(k == outcome))**2 for k, v in old.items())
        except (requests.RequestException, ValueError, KeyError, TypeError,
                ZeroDivisionError) as exc:
            result['errors'] += 1
            log.warning('3.6 backtest skipped %s game %s: %s', sport, game.get('id'), exc)
    return result


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔬 OddX2 3.7 | Running extended historical validation (up to 45 days)...')
    lines = ['🔬 OddX2 3.7-beta | EXTENDED VALIDATION',
             'Read-only. Same historical fixtures for 3.5 and 3.6 models.',
             'No historical bookmaker prices, betting ROI, or saved records.']
    for sport in ('basketball', 'hockey'):
        try:
            r = await asyncio.to_thread(ms_rolling_backtest, sport)
            n = r['tested']
            if n:
                lines.append(
                    f'{sport.title()}: {n} eligible games | {r["scanned"]} examined | '
                    f'API errors {r["errors"]}\n'
                    f'3.6 Brier {r["new_brier"]/n:.3f} | accuracy {r["correct"]/n:.1%}\n'
                    f'3.5 Brier {r["old_brier"]/n:.3f} | accuracy {r["old_correct"]/n:.1%}\n'
                    f'Naive baseline Brier {r["baseline_brier"]/n:.3f}'
                )
                ranked = sorted(r['leagues'].items(), key=lambda kv: kv[1]['n'], reverse=True)
                league_lines = [f'{name}: {v["n"]} games | 3.6 Brier {v["new"]/v["n"]:.3f} '
                                f'vs 3.5 {v["old"]/v["n"]:.3f}'
                                for name, v in ranked[:5]]
                if league_lines:
                    lines.append(f'{sport.title()} league breakdown (largest samples):\n' +
                                 '\n'.join(league_lines))
            else:
                lines.append(f'{sport.title()}: insufficient eligible games; errors {r["errors"]}.')
        except Exception as exc:
            log.exception('3.7 validation failed for %s', sport)
            lines.append(f'{sport.title()}: unavailable ({type(exc).__name__}).')
    lines.append('Lower Brier is better. Basketball threshold is model-derived, NOT '
                 'a historical bookmaker line. Hockey: regulation-time 3-way. '
                 'Results are exploratory; sample selection and coverage may bias comparisons.')
    message = '\n\n'.join(lines)
    for i in range(0, len(message), 3900):
        await update.message.reply_text(message[i:i+3900])


async def marketcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inspect current quoted lines without storing them as historical odds."""
    await update.message.reply_text('📊 OddX2 3.7.1 | Checking currently offered multisport lines...')
    lines = ['📊 OddX2 3.8-beta | LIVE LINE AUDIT',
             'Research only. These are current quotes, NOT historical pre-match prices.']
    for sport in ('basketball', 'hockey'):
        try:
            candidates, stat = await asyncio.to_thread(ms_collect_candidates, sport, datetime.now(timezone.utc))
            events = {}
            for c in candidates:
                key = c['fixture']
                if key not in events or c['difference'] > events[key]['difference']:
                    events[key] = c
            lines.append(f'{sport.title()}: {stat["inspected"]} games checked, '
                         f'{stat["priced"]} priced, {stat["unique_events_passed"]} '
                         f'events passing experimental filter; API errors {stat["errors"]}.')
            for c in sorted(events.values(), key=lambda v: v['difference'], reverse=True)[:4]:
                lines.append(f'{c["home"]} vs {c["away"]} | {c["bookmaker"]} | '
                             f'{c["selection"]} @ {c["odd"]:.2f} | '
                             f'model {c["estimated"]:.1%} | gap {c["difference"]:+.1%}')
        except Exception as exc:
            log.exception('3.7 live line audit failed: %s', sport)
            lines.append(f'{sport.title()}: unavailable ({type(exc).__name__}).')
    lines.append('Uncalibrated probabilities. Basketball overtime rules remain unverified. '
                 'No odds history, ROI, bets, or channel posts created.')
    message = '\n\n'.join(lines)
    for i in range(0, len(message), 3900):
        await update.message.reply_text(message[i:i+3900])






async def weekendtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔬 OddX2 | Scanning the next 72 hours for Direct EV and Reverse Signal. Read-only; this may take several minutes.')
    try:
        stats,direct,reverse=await asyncio.to_thread(weekend_research)
        lines=['🔬 OddX2 5.2 | WEEKEND RESEARCH',
            'READ-ONLY: no posts, bets, database reset or ROI changes.',
            f'Next 72h: {stats["fixtures"]} fixtures found; {stats["checked"]} checked; '
            f'{stats["history"]} with enough history; {stats["priced"]} priced; API errors {stats["errors"]}.',
            f'Direct EV: {len(direct)} distinct matches | Reverse Signal: {len(reverse)} distinct matches.',
            'DIRECT EV (1.80–2.20; model EV >= +5%, uncalibrated):']
        for x in direct[:30]:
            lines.append(f'{x["kick"].astimezone(LOCAL):%d %b %H:%M} | {x["home"]} vs {x["away"]} | {x["selection"]} @ {x["odd"]:.2f} | model EV {x["model_ev"]:+.1%} | {x["bookmaker"]} | samples {x["home_n"]}/{x["away_n"]}')
        lines.append('REVERSE SIGNAL (observational only; opposite quote actually available):')
        for x in reverse[:30]:
            lines.append(f'{x["kick"].astimezone(LOCAL):%d %b %H:%M} | {x["home"]} vs {x["away"]} | {x["selection"]} @ {x["odd"]:.2f} | opposite of {x["original"]} @ {x["original_odd"]:.2f} | {x["bookmaker"]}')
        lines.append('No implied guarantee. Reverse is not model-positive EV by definition. Prices may change. No backtest or calibrated ROI yet.')
        text='\n'.join(lines)
        for i in range(0,len(text),3600):
            await update.message.reply_text(text[i:i+3600])
    except Exception:
        log.exception('Weekend research failed')
        await update.message.reply_text('Weekend research failed. Check Railway logs. Existing data unchanged.')

def direct_research_53():
    """Read-only 72h weekend discovery; no existing picks or ROI touched.

    Both branches use actual bookmaker quotes; reverse is a market-disagreement
    hypothesis, NOT a mathematically proven positive-EV pick.
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=72)
    dates = [(now + timedelta(days=i)).date().isoformat() for i in range(4)]
    fixtures_by_id = {}
    for day in dates:
        for game in api('fixtures', {'date': day, 'timezone': 'UTC'}):
            stamp = (game.get('fixture') or {}).get('date')
            fid = (game.get('fixture') or {}).get('id')
            if not stamp or not fid:
                continue
            kick = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)
            if now + timedelta(minutes=MIN_LEAD_MINUTES+10) < kick <= end:
                fixtures_by_id[fid] = (kick, game)
    selected = prioritize_football_fixtures(sorted(fixtures_by_id.values(), key=lambda v:v[0]))
    # Avoid excessive requests and long Telegram timeouts: 160 fixture cap.
    selected = selected[:180]
    cache, direct, reverse = {}, [], []
    stats = {'fixtures':len(fixtures_by_id),'checked':0,'history':0,'priced':0,'errors':0}
    for kick, game in selected:
        teams = game.get('teams') or {}
        h, a = teams.get('home') or {}, teams.get('away') or {}
        if not h.get('id') or not a.get('id'):
            continue
        stats['checked'] += 1
        try:
            home = [g for g in recent_games(h['id'],cache) if g['venue']=='home'][:10]
            away = [g for g in recent_games(a['id'],cache) if g['venue']=='away'][:10]
            if len(home)<5 or len(away)<5:
                continue
            stats['history'] += 1
            prices = odds_for(kick,game)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            stats['errors'] += 1
            log.warning('Weekend research skip: %s', exc)
            continue
        if not prices:
            continue
        stats['priced'] += 1
        raw_h = (sum(g['for'] for g in home)/len(home)+sum(g['against'] for g in away)/len(away))/2
        raw_a = (sum(g['for'] for g in away)/len(away)+sum(g['against'] for g in home)/len(home))/2
        lh = max(.15,min(.8*raw_h+.2*1.40,4.0))
        la = max(.15,min(.8*raw_a+.2*1.15,4.0))
        for entry in prices:
            for book in entry.get('bookmakers',[]):
                for bet in book.get('bets',[]):
                    outcomes = {}
                    for value in bet.get('values',[]):
                        label = str(value.get('value','')).strip()
                        market = exact_goal_market(bet.get('id'),label)
                        if market is None:
                            continue
                        try: odd = float(value.get('odd'))
                        except (ValueError,TypeError): continue
                        if math.isfinite(odd) and odd>1:
                            outcomes[(market[0],market[1],market[2])] = (label,odd,market)
                    for key,(label,odd,market) in outcomes.items():
                        estimated = goal_probability(market,lh,la)
                        ev = estimated*odd-1
                        base = {'fixture':game['fixture']['id'],'home':h.get('name','?'),
                            'away':a.get('name','?'),'league':league_label(game),
                            'kick':kick,'bookmaker':book.get('name','?'),
                            'home_n':len(home),'away_n':len(away)}
                        if 1.50<=odd<=2.20 and ev>=.05 and estimated - 1/odd >= .05:
                            direct.append(dict(base,selection=label,odd=odd,model_ev=ev,estimated=estimated,implied=1/odd,market=bet.get("name","?"),market_kind=market[0],positive=market[1],threshold=market[2]))
                        if False:  # Reverse Signal discontinued
                            opposite = outcomes.get((market[0],not market[1],market[2]))
                            if opposite and 1.20<=opposite[1]<=2.20:
                                # Reverse is tracked as disagreement, not positive model EV.
                                reverse.append(dict(base,selection=opposite[0],odd=opposite[1],
                                    original=label,original_odd=odd,model_ev=goal_probability(opposite[2],lh,la)*opposite[1]-1))
    def unique_ranked(items):
        by_event={}
        for item in items:
            fid=item['fixture']
            score=(item['model_ev'] if 'original' not in item else item['original_odd']/max(1.01,1/item['model_ev']+1) if item['model_ev']>0 else item['original_odd'])
            if fid not in by_event or score>by_event[fid][0]:
                by_event[fid]=(score,item)
        return [x[1] for x in sorted(by_event.values(),key=lambda x:x[0],reverse=True)]
    return stats,unique_ranked(direct)



# OddX2 5.3: independent public research cohort; historical tables never erased.
# Every published event is one fixed hypothetical unit. No bets are placed.
V53_DAILY_CAP = 40
V53_PER_SCAN_CAP = 15
V53_SCAN_INTERVAL_HOURS = 3
V53_MAX_SCANS_DAY = 10


def v53_db():
    con = singles_db()
    con.execute("""CREATE TABLE IF NOT EXISTS direct_picks_53 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fixture_id INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        published_at TEXT,
        result_posted_at TEXT,
        kickoff TEXT NOT NULL,
        league TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
        bookmaker TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
        market_kind TEXT NOT NULL, positive INTEGER NOT NULL, threshold REAL,
        odd REAL NOT NULL, estimated REAL NOT NULL, implied REAL NOT NULL,
        estimated_ev REAL NOT NULL, home_n INTEGER NOT NULL, away_n INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        home_goals INTEGER, away_goals INTEGER
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS direct_scans_53 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at TEXT NOT NULL,
        inspected INTEGER NOT NULL, history_ok INTEGER NOT NULL,
        priced INTEGER NOT NULL, candidates INTEGER NOT NULL,
        saved INTEGER NOT NULL, status TEXT NOT NULL
    )""")
    con.commit()
    return con


def v53_scan():
    now = datetime.now(timezone.utc)
    today = now.astimezone(LOCAL).date()
    with v53_db() as con:
        runs = con.execute('SELECT scanned_at FROM direct_scans_53').fetchall()
        saved = con.execute('SELECT created_at FROM direct_picks_53').fetchall()
        used = {r[0] for r in con.execute('SELECT fixture_id FROM direct_picks_53')}
    n_runs = sum(datetime.fromisoformat(r[0]).astimezone(LOCAL).date()==today for r in runs)
    n_saved = sum(datetime.fromisoformat(r[0]).astimezone(LOCAL).date()==today for r in saved)
    if n_runs >= V53_MAX_SCANS_DAY:
        return 'Daily scan limit reached (API quota protection).'
    if n_saved >= V53_DAILY_CAP:
        return 'Daily research cap reached.'
    stats, candidates = direct_research_53()
    count=0
    with v53_db() as con:
        for c in candidates:
            if c['fixture'] in used or count>=min(V53_PER_SCAN_CAP,V53_DAILY_CAP-n_saved):
                continue
            # Never publish already started events or odds discovered too late.
            if c['kick'] <= datetime.now(timezone.utc)+timedelta(minutes=MIN_LEAD_MINUTES+10):
                continue
            cur=con.execute("""INSERT OR IGNORE INTO direct_picks_53 (
                fixture_id,created_at,kickoff,league,home,away,bookmaker,market,
                selection,market_kind,positive,threshold,odd,estimated,implied,
                estimated_ev,home_n,away_n)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                c['fixture'],now.isoformat(),c['kick'].isoformat(),c['league'],c['home'],c['away'],
                c['bookmaker'],c['market'],c['selection'],c['market_kind'],int(c['positive']),
                c['threshold'],c['odd'],c['estimated'],c['implied'],c['model_ev'],
                c['home_n'],c['away_n']))
            count += cur.rowcount
            if cur.rowcount:
                used.add(c['fixture'])
        con.execute('INSERT INTO direct_scans_53 (scanned_at,inspected,history_ok,priced,candidates,saved,status) VALUES (?,?,?,?,?,?,?)',
                    (now.isoformat(),stats['checked'],stats['history'],stats['priced'],len(candidates),count,'ok'))
    return f'Saved {count} new selections. Checked {stats["checked"]} fixtures; {stats["priced"]} with odds; {len(candidates)} qualifying events; API errors {stats["errors"]}.'


async def v53_publish(app):
    if not CHANNEL_ID:
        return 0
    now=datetime.now(timezone.utc)
    with v53_db() as con:
        rows=con.execute("""SELECT id,kickoff,league,home,away,bookmaker,market,
            selection,odd,estimated,implied,estimated_ev,home_n,away_n
            FROM direct_picks_53 WHERE published_at IS NULL ORDER BY id""").fetchall()
    count=0
    for row in rows:
        (id_,kick,league,home,away,book,market,selection,odd,estimated,implied,ev,hn,an)=row
        kickoff=datetime.fromisoformat(kick).astimezone(timezone.utc)
        if kickoff <= datetime.now(timezone.utc)+timedelta(minutes=MIN_LEAD_MINUTES):
            continue
        msg=(f'📊 OddX2 5.3 | DIRECT EV #{id_}\n'
             f'{home} vs {away}\n{league}\n'
             f'Kickoff: {kickoff.astimezone(LOCAL):%d %b %Y %H:%M} Bucharest\n'
             f'{market}: {selection} @ {odd:.2f} | {book}\n'
             f'Model probability: {estimated:.1%} | Implied: {implied:.1%}\n'
             f'Model EV (uncalibrated): {ev:+.1%}\n'
             f'Historical sample: {hn} home / {an} away\n'
             '⚠️ Experimental statistical research only. Model EV is not proven value; odds may change. No guaranteed returns.')
        try:
            await app.bot.send_message(chat_id=CHANNEL_ID,text=msg)
        except Exception:
            log.exception('5.3 publish failed for #%s',id_)
            break
        with v53_db() as con:
            con.execute('UPDATE direct_picks_53 SET published_at=? WHERE id=? AND published_at IS NULL',
                        (datetime.now(timezone.utc).isoformat(),id_))
        count+=1
    return count


def v53_settle():
    with v53_db() as con:
        rows=con.execute("SELECT id,fixture_id,market_kind,positive,threshold FROM direct_picks_53 WHERE status='pending' AND published_at IS NOT NULL").fetchall()
    changed=0
    for id_,fixture,kind,positive,threshold in rows:
        try:
            games=api('fixtures',{'id':fixture})
        except Exception:
            log.exception('5.3 settlement API failed for fixture %s',fixture)
            continue
        if not games:
            continue
        game=games[0]
        status=(game.get('fixture') or {}).get('status',{}).get('short')
        if status in ('CANC','ABD','AWD','WO'):
            result,hg,ag='void',None,None
        elif status=='FT':
            goals=game.get('goals') or {}
            hg,ag=goals.get('home'),goals.get('away')
            if not isinstance(hg,int) or not isinstance(ag,int):
                continue
            if kind=='goals':
                hit=((hg+ag)>threshold)==bool(positive)
            elif kind=='btts':
                hit=((hg>0 and ag>0)==bool(positive))
            else:
                continue
            result='won' if hit else 'lost'
        else:
            # Postponements are left pending until their final status is known.
            continue
        with v53_db() as con:
            con.execute("UPDATE direct_picks_53 SET status=?,home_goals=?,away_goals=? WHERE id=? AND status='pending'",(result,hg,ag,id_))
        changed+=1
    return changed


async def v53_publish_results(app):
    if not CHANNEL_ID:
        return
    with v53_db() as con:
        rows=con.execute("""SELECT id,home,away,status,odd,home_goals,away_goals
            FROM direct_picks_53 WHERE published_at IS NOT NULL AND result_posted_at IS NULL
            AND status IN ('won','lost','void') ORDER BY id""").fetchall()
    for id_,home,away,status,odd,hg,ag in rows:
        pnl=odd-1 if status=='won' else (-1 if status=='lost' else 0)
        msg=(f'🏁 OddX2 5.3 | RESULT #{id_}\n{home} vs {away}\n'
             f'Final score: {hg if hg is not None else "-"}-{ag if ag is not None else "-"}\n'
             f'{status.upper()} | Recorded odds: {odd:.2f}\n'
             f'Hypothetical 1-unit P/L: {pnl:+.2f} units\n'
             'Research tracking only. No wagers placed.')
        try:
            await app.bot.send_message(chat_id=CHANNEL_ID,text=msg)
        except Exception:
            log.exception('5.3 result post failed for #%s',id_)
            break
        with v53_db() as con:
            con.execute('UPDATE direct_picks_53 SET result_posted_at=? WHERE id=? AND result_posted_at IS NULL',
                        (datetime.now(timezone.utc).isoformat(),id_))


def v53_report(period='all'):
    now=datetime.now(LOCAL)
    if period=='today':
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        end=start+timedelta(days=1)
        label=f'{start:%d %b %Y}'
    elif period=='week':
        end=(now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        start=end-timedelta(days=7)
        label=f'{start:%d %b}–{(end-timedelta(days=1)):%d %b %Y}'
    elif period=='month':
        end=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        start=(end-timedelta(days=1)).replace(day=1)
        label=f'{start:%B %Y}'
    else:
        start=end=None
        label='All time (5.3 only)'
    with v53_db() as con:
        rows=con.execute('SELECT published_at,status,odd,estimated_ev FROM direct_picks_53 WHERE published_at IS NOT NULL').fetchall()
    if start:
        rows=[r for r in rows if start<=datetime.fromisoformat(r[0]).astimezone(LOCAL)<end]
    def line(group,label):
        wins=sum(r[1]=='won' for r in group)
        losses=sum(r[1]=='lost' for r in group)
        voids=sum(r[1]=='void' for r in group)
        pending=sum(r[1]=='pending' for r in group)
        settled=wins+losses
        pnl=sum((r[2]-1 if r[1]=='won' else -1) for r in group if r[1] in ('won','lost'))
        roi=f'{pnl/settled:+.1%}' if settled else 'N/A'
        hit=f'{wins/settled:.1%}' if settled else 'N/A'
        return (f'{label}: {len(group)} published | {wins}W {losses}L {voids}V {pending}P\n'
                f'  Win rate: {hit} | P/L: {pnl:+.2f}u | ROI: {roi}')
    lines=['📈 OddX2 5.3 | DIRECT EV PERFORMANCE',f'Period: {label}',line(rows,'TOTAL')]
    for low,high,label in [(1.50,1.70,'1.50–1.69'),(1.70,1.90,'1.70–1.89'),(1.90,2.201,'1.90–2.20')]:
        lines.append(line([r for r in rows if low<=r[2]<high],label))
    lines.append('Published picks only; 1 hypothetical unit per settled pick. Pending/void excluded from ROI. Model EV uncalibrated. Previous strategies excluded.')
    return '\n'.join(lines)


async def value_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 OddX2 5.3 | Scanning 72h for Direct EV 1.50–2.20. This may take several minutes...')
    try:
        result=await asyncio.to_thread(v53_scan)
        posted=await v53_publish(context.application)
        await update.message.reply_text(f'{result} Published to channel: {posted}. /report for new-strategy statistics.')
    except Exception:
        log.exception('5.3 manual scan failed')
        await update.message.reply_text('5.3 scan failed; check Railway logs. Historical data preserved.')


async def value_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(v53_report,'all'))


async def value_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(v53_report,'today'))


async def value_weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(v53_report,'week'))


async def value_monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(v53_report,'month'))


async def value_picks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with v53_db() as con:
        rows=con.execute('SELECT id,home,away,selection,odd,status FROM direct_picks_53 WHERE published_at IS NOT NULL ORDER BY id DESC LIMIT 20').fetchall()
    msg='📋 OddX2 5.3 | RECENT DIRECT EV\n'+('\n'.join(f'#{i} {h} vs {a} | {sel} @ {odd:.2f} | {st.upper()}' for i,h,a,sel,odd,st in rows) if rows else 'No published selections yet.')
    await update.message.reply_text(msg[:3900])


async def weekendtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔬 Scanning 72h, Direct EV 1.50–2.20 only. Read-only; may take several minutes.')
    try:
        stats,picks=await asyncio.to_thread(direct_research_53)
        lines=[f'OddX2 5.3 | DIRECT RESEARCH: {stats["fixtures"]} fixtures, {stats["checked"]} checked, {stats["priced"]} priced, {len(picks)} qualifying; errors {stats["errors"]}.']
        for x in picks[:40]:
            lines.append(f'{x["kick"].astimezone(LOCAL):%d %b %H:%M} | {x["home"]} vs {x["away"]} | {x["selection"]} @ {x["odd"]:.2f} | model EV {x["model_ev"]:+.1%}')
        msg='\n'.join(lines)
        for i in range(0,len(msg),3500):
            await update.message.reply_text(msg[i:i+3500])
    except Exception:
        log.exception('5.3 weekend test failed')
        await update.message.reply_text('Weekend research failed. See Railway logs.')


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('✅ OddX2 5.4 | Direct EV research active. Odds 1.50–2.20. Continuing 5.3 ROI cohort (no reset). /scan /picks /report /markets /quality /leagues /today /weekly /monthly')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await status(update,context)


async def periodic_scanning(app):
    await asyncio.sleep(300)
    while True:
        try:
            state=await asyncio.to_thread(v53_scan)
            posted=await v53_publish(app)
            log.info('5.3 auto scan: %s; published=%s',state,posted)
        except Exception:
            log.exception('5.3 automatic scan failed')
        await asyncio.sleep(V53_SCAN_INTERVAL_HOURS*3600)


async def periodic_settlement(app):
    while True:
        await asyncio.sleep(1800)
        try:
            await asyncio.to_thread(v53_settle)
            await v53_publish_results(app)
            # Old records remain intact, but old results are not republished.
        except Exception:
            log.exception('5.3 settlement failed')


async def scheduled_reports(app):
    while True:
        try:
            now=datetime.now(LOCAL)
            if CHANNEL_ID and now.hour>=23:
                key=now.strftime('%Y-%m-%d')
                with db_connect() as con:
                    already=con.execute('SELECT 1 FROM report_posts WHERE period_type=? AND period_key=?',('v53_daily',key)).fetchone()
                if not already:
                    await app.bot.send_message(chat_id=CHANNEL_ID,text=await asyncio.to_thread(v53_report,'today'))
                    with db_connect() as con:
                        con.execute('INSERT OR IGNORE INTO report_posts(period_type,period_key,sent_at) VALUES(?,?,?)',
                                    ('v53_daily',key,datetime.now(timezone.utc).isoformat()))
            if CHANNEL_ID and now.hour>=9:
                for kind,due in [('week',now.weekday()==0),('month',now.day==1)]:
                    if not due: continue
                    key=((now-timedelta(days=7)).strftime('%G-W%V') if kind=='week' else (now.replace(day=1)-timedelta(days=1)).strftime('%Y-%m'))
                    with db_connect() as con:
                        already=con.execute('SELECT 1 FROM report_posts WHERE period_type=? AND period_key=?',('v53_'+kind,key)).fetchone()
                    if not already:
                        await app.bot.send_message(chat_id=CHANNEL_ID,text=await asyncio.to_thread(v53_report,kind))
                        with db_connect() as con:
                            con.execute('INSERT OR IGNORE INTO report_posts(period_type,period_key,sent_at) VALUES(?,?,?)',
                                        ('v53_'+kind,key,datetime.now(timezone.utc).isoformat()))
        except Exception:
            log.exception('5.3 scheduled report failed')
        await asyncio.sleep(3600)


async def on_startup(app):
    await asyncio.to_thread(lambda: v53_db().close())
    app.create_task(periodic_settlement(app))
    app.create_task(periodic_scanning(app))
    app.create_task(scheduled_reports(app))
    app.create_task(periodic_sport_observations(app))
    await v53_publish(app)


def main():
    if not TOKEN or not KEY:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or API_FOOTBALL_KEY in Railway')
    app = Application.builder().token(TOKEN).post_init(on_startup).build()
    for command, handler in [('start', start), ('status', status),
                             ('fixtures', meciuri), ('meciuri', meciuri),
                             ('odds', cote), ('cote', cote),
                             ('double', legacy_notice), ('dubla', legacy_notice),
                             ('analyze', legacy_notice), ('analiza', legacy_notice),
                             ('history', value_picks_command), ('istoric', value_picks_command), ('picks', value_picks_command),
                             ('stats', value_report_command), ('statistici', value_report_command),
                             ('results', value_picks_command), ('verifica', value_picks_command),
                             ('scan', value_scan_command), ('scanare', value_scan_command),
                             ('report', value_report_command), ('raport', value_report_command), ('today', value_today_command),
                             ('weekly', value_weekly_command), ('monthly', value_monthly_command),
                             ('alltime', value_report_command), ('markets', v54_market_command), ('quality', v54_quality_command), ('leagues', v54_league_command), ('diagnostics', diagnostics), ('footballaudit', footballaudit), ('pairaudit', pairaudit),
                             ('contextaudit', contextaudit_command), ('weekendtest', weekendtest_command), ('sports', sports), ('basketball', basketball_preview), ('hockey', hockey_preview), ('multiscan', multiscan_command), ('backtest', backtest_command), ('marketcheck', marketcheck_command)]:
        app.add_handler(CommandHandler(command, handler))
    log.info('OddX2 5.4-direct-ev-diagnostics started')
    app.run_polling()


if __name__ == '__main__':
    main()
