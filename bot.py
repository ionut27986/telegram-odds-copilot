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
log = logging.getLogger('odds-copilot')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
KEY = os.getenv('API_FOOTBALL_KEY')
URL = 'https://v3.football.api-sports.io'
LOCAL = ZoneInfo('Europe/Bucharest')
DB_PATH = Path(os.getenv('ODDS_DB_PATH', '/data/odds_copilot.sqlite3'))
AUTO_SCAN_HOURS = 2
SCAN_LIMIT_PER_DAY = 16
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
VERSION = "3.5-beta"
MIN_LEAD_MINUTES = 15
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


def analyze_batch(max_doubles=3):
    """Rank multiple independent experimental football doubles."""
    cache, choices = {}, []
    inspected, qualified = 0, 0
    for kick, game in fixtures(20)[:45]:
        if kick <= datetime.now(timezone.utc) + timedelta(minutes=MIN_LEAD_MINUTES + 2):
            continue
        teams = game.get('teams', {})
        h, a = teams.get('home', {}), teams.get('away', {})
        if not h.get('id') or not a.get('id'):
            continue
        inspected += 1
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
                        if not math.isfinite(odd) or not 1.25 <= odd <= 1.85:
                            continue
                        estimated = goal_probability(market, home_lambda, away_lambda)
                        implied = 1 / odd
                        difference = estimated - implied
                        if difference < 0.05:
                            continue
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
                        })
    unique = {}
    for c in choices:
        key = (c['fixture'], c['market'], c['selection'], c['bookmaker'])
        if key not in unique or c['odd'] > unique[key]['odd']:
            unique[key] = c
    choices = sorted(unique.values(), key=lambda c: c['difference'], reverse=True)[:150]
    with db_connect() as con:
        used = {r[0] for r in con.execute('SELECT fixture_id FROM legs')}
    choices = [c for c in choices if c['fixture'] not in used]
    ranked = []
    for x, y in combinations(choices, 2):
        if x['fixture'] == y['fixture'] or x['bookmaker'] != y['bookmaker']:
            continue
        combined = x['odd'] * y['odd']
        if not 1.90 <= combined <= 2.10:
            continue
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
    await update.message.reply_text('🤖 OddX2 3.5-beta online!\n/status - status\n/fixtures - upcoming fixtures\n/odds - bookmaker odds\n/double - mathematical test\n/analyze - statistical model\n/history - saved doubles\n/stats - results and ROI\n/results - refresh scores\n/scan - scan and publish\n/report - daily report\n/weekly - last completed week\n/monthly - last completed month\n/alltime - published track record\n/diagnostics - scan reasons\n/sports - check basketball and hockey feeds\n/basketball - basketball research preview\n/hockey - hockey research preview\n/multiscan - multisport diagnostic research')


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('✅ OddX2 3.5-beta is online.')


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
    await update.message.reply_text('🔎 OddX2 3.1 — Running statistical analysis...')
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
        lines = ['🧪 OddX2 3.1 — Poisson model', 'Experimental statistical analysis', '']
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
                                              (kind,period_key)).fetchone()
                    if already:
                        continue
                    message = await asyncio.to_thread(performance_report,kind)
                    await app.bot.send_message(chat_id=CHANNEL_ID, text=message)
                    with db_connect() as con:
                        con.execute('INSERT OR IGNORE INTO report_posts(period_type,period_key,sent_at) VALUES(?,?,?)',
                                    (kind,period_key,datetime.now(timezone.utc).isoformat()))
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
            state = await asyncio.to_thread(automated_scan)
            log.info('Automated research scan: %s', state)
            await publish_updates(app)
        except Exception:
            log.exception('Research scan failed')
        await asyncio.sleep(AUTO_SCAN_HOURS * 3600)


async def periodic_settlement(app):
    while True:
        await asyncio.sleep(1800)
        try:
            await asyncio.to_thread(settle_pending)
            await publish_results_and_summary(app)
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
    app.create_task(publish_updates(app))


def db_connect_and_close():
    con = db_connect()
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
def ms_probabilities(sport, home, away, expected_home, expected_away, market, label):
    """Return exploratory probability only for an explicitly understood model outcome."""
    if sport == 'basketball':
        if market != 4:
            return None
        m = re.fullmatch(r'(Over|Under)\s+(\d+\.5)', label, re.I)
        if not m:
            return None
        # Empirical volatility of historical game totals; conservative floor.
        totals = [r[2]+r[3] for r in home] + [r[2]+r[3] for r in away]
        if len(totals) < 10:
            return None
        avg = sum(totals)/len(totals)
        sd = math.sqrt(sum((v-avg)**2 for v in totals)/(len(totals)-1))
        sd = max(sd, 15.0)
        line = float(m.group(2))
        z = (line - (expected_home+expected_away))/sd
        under = 0.5*(1+math.erf(z/math.sqrt(2)))
        return (1-under if m.group(1).lower() == 'over' else under), 'full-game total; OT rules unverified'
    if sport == 'hockey':
        # Market 1 is explicitly 3Way Result (regulation), not two-way incl. OT.
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
        return probs[label], '60-minute 3-way; Poisson research model'
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
    await update.message.reply_text('🔎 OddX2 3.5 | Running read-only multisport diagnostic scan (48h, up to 60 games per sport)...')
    try:
        pairs,stats,diag = await asyncio.to_thread(multiscan_preview)
        lines = ['🧪 OddX2 3.5-beta | MULTISPORT DIAGNOSTICS',
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


def ms_rolling_backtest(sport, days=14, max_tests=16):
    """Read-only, leakage-free rolling test of model probabilities, NOT a betting ROI backtest.

    Basketball uses a predeclared half-point line derived from pregame historical
    expected total. Hockey tests 60-minute 3-way outcome. Neither uses historical odds.
    """
    now = datetime.now(timezone.utc)
    cache, games, seen = {}, [], set()
    for day_back in range(1, days+1):
        date = (now - timedelta(days=day_back)).date().isoformat()
        for g in multisport_api(sport, 'games', {'date':date}):
            if g.get('id') in seen or ms_finished_score(sport, g) is None:
                continue
            seen.add(g['id'])
            games.append(g)
    # Recent completed events, deterministic order; no current/future results in training.
    games.sort(key=lambda g:g.get('date',''), reverse=True)
    result = {'tested':0, 'eligible':0, 'errors':0, 'brier_sum':0.0,
              'baseline_brier_sum':0.0, 'correct':0, 'sport':sport}
    for g in games[:100]:
        if result['tested'] >= max_tests:
            break
        try:
            league, teams = g.get('league') or {}, g.get('teams') or {}
            h, a = teams.get('home') or {}, teams.get('away') or {}
            if not all((h.get('id'),a.get('id'),league.get('id'),league.get('season') is not None)):
                continue
            when = datetime.fromisoformat(g['date'].replace('Z','+00:00')).astimezone(timezone.utc)
            home = [r for r in ms_history(sport,h['id'],league['season'],league['id'],when,cache)
                    if r[1]=='home'][:10]
            away = [r for r in ms_history(sport,a['id'],league['season'],league['id'],when,cache)
                    if r[1]=='away'][:10]
            if len(home)<5 or len(away)<5:
                continue
            result['eligible'] += 1
            eh = (sum(r[2] for r in home)/len(home)+sum(r[3] for r in away)/len(away))/2
            ea = (sum(r[2] for r in away)/len(away)+sum(r[3] for r in home)/len(home))/2
            hs,as_ = ms_finished_score(sport,g)
            if sport=='basketball':
                # A model-derived test threshold, NOT a historical bookmaker line.
                line = math.floor(eh+ea)+0.5
                outcome = int(hs+as_ > line)
                pr = ms_probabilities(sport,home,away,eh,ea,4,f'Over {line:.1f}')
                if pr is None: continue
                probability = pr[0]
                baseline = 0.5
            else:
                outcome = 'Home' if hs>as_ else 'Away' if hs<as_ else 'Draw'
                probs = {label:ms_probabilities(sport,home,away,eh,ea,1,label)[0]
                         for label in ('Home','Draw','Away')}
                # Multi-class Brier score: sum of squared probability errors.
                probability = probs[outcome]
                baseline = 1/3
            if sport=='basketball':
                result['brier_sum'] += (probability-outcome)**2
                result['baseline_brier_sum'] += (baseline-outcome)**2
                result['correct'] += int((probability>=0.5)==bool(outcome))
            else:
                result['brier_sum'] += sum((p-int(k==outcome))**2 for k,p in probs.items())
                result['baseline_brier_sum'] += 2/3
                result['correct'] += int(max(probs,key=probs.get)==outcome)
            result['tested'] += 1
        except (requests.RequestException, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            result['errors'] += 1
            log.warning('3.5 rolling test skipped %s game %s: %s',sport,g.get('id'),exc)
    return result


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔬 OddX2 3.5 | Running read-only rolling historical evaluation...')
    lines = ['🔬 OddX2 3.5-beta | ROLLING MODEL CHECK',
             'No bookmaker odds or historical betting ROI. No records saved or published.',
             'Each prediction uses only games finished BEFORE its kickoff.']
    for sport in ('basketball','hockey'):
        try:
            r = await asyncio.to_thread(ms_rolling_backtest,sport)
            n = r['tested']
            if n:
                lines.append(f'{sport.title()}: {n} historical games tested | '
                             f'accuracy {r["correct"]/n:.1%} | '
                             f'model Brier {r["brier_sum"]/n:.3f} | '
                             f'naive baseline Brier {r["baseline_brier_sum"]/n:.3f} | '
                             f'errors {r["errors"]}')
            else:
                lines.append(f'{sport.title()}: insufficient eligible historical games; errors {r["errors"]}.')
        except Exception as exc:
            log.exception('3.5 backtest failed for %s',sport)
            lines.append(f'{sport.title()}: unavailable ({type(exc).__name__}).')
    lines.append('Basketball: over/under a MODEL-DERIVED threshold (not historical market odds). '
                 'Hockey: 60-minute 3-way result. Tiny samples are not calibration or proof of advantage.')
    await update.message.reply_text('\n\n'.join(lines)[:4000])


def main():
    if not TOKEN or not KEY:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or API_FOOTBALL_KEY in Railway')
    app = Application.builder().token(TOKEN).post_init(on_startup).build()
    for command, handler in [('start', start), ('status', status),
                             ('fixtures', meciuri), ('meciuri', meciuri),
                             ('odds', cote), ('cote', cote),
                             ('double', dubla), ('dubla', dubla),
                             ('analyze', analiza), ('analiza', analiza),
                             ('history', istoric), ('istoric', istoric),
                             ('stats', statistici), ('statistici', statistici),
                             ('results', verifica), ('verifica', verifica),
                             ('scan', scanare), ('scanare', scanare),
                             ('report', raport), ('raport', raport),
                             ('weekly', weekly), ('monthly', monthly),
                             ('alltime', alltime), ('diagnostics', diagnostics),
                             ('sports', sports), ('basketball', basketball_preview), ('hockey', hockey_preview), ('multiscan', multiscan_command), ('backtest', backtest_command)]:
        app.add_handler(CommandHandler(command, handler))
    log.info('OddX2 3.5-beta started')
    app.run_polling()


if __name__ == '__main__':
    main()
