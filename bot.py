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


def analyze():
    """Experimental Poisson estimates; not calibrated betting predictions."""
    cache, choices = {}, []
    inspected, qualified = 0, 0
    for kick, game in fixtures(6)[:12]:
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
    best, best_score = None, float('-inf')
    for x, y in combinations(choices, 2):
        if x['fixture'] == y['fixture'] or x['bookmaker'] != y['bookmaker']:
            continue
        combined = x['odd'] * y['odd']
        if not 1.90 <= combined <= 2.10:
            continue
        score = x['difference'] + y['difference'] - 0.1 * abs(combined - 2)
        if score > best_score:
            best, best_score = (x, y, combined), score
    return best, inspected, qualified, len(choices)


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
    await update.message.reply_text('🤖 Odds Copilot 2.3 online!\n/status - status\n/meciuri - upcoming fixtures\n/cote - sample odds\n/dubla - mathematical 1X2 test\n/analiza - Poisson model\n/istoric - saved doubles\n/statistici - ROI and results\n/verifica - refresh final scores')


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('✅ Odds Copilot 2.3 funcționează.')


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
    await update.message.reply_text('🔎 Odds Copilot 2.3 — Running Poisson model...')
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
        lines = ['🧪 Odds Copilot 2.3 — Poisson model', 'Experimental statistical analysis', '']
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
            '⚠️ Experimental and uncalibrated. Not proven betting value. No automatic publishing.',
        ])
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
        await update.message.reply_text(f'🔄 Updated {updated} settled selections. Use /statistici.')
    except Exception:
        log.exception('Settlement failed')
        await update.message.reply_text('❌ Settlement error. Check Railway logs.')


async def periodic_settlement(app):
    while True:
        await asyncio.sleep(1800)
        try:
            await asyncio.to_thread(settle_pending)
        except Exception:
            log.exception('Background settlement failed')


async def on_startup(app):
    await asyncio.to_thread(db_connect_and_close)
    app.create_task(periodic_settlement(app))


def db_connect_and_close():
    con = db_connect()
    con.close()


def main():
    if not TOKEN or not KEY:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or API_FOOTBALL_KEY in Railway')
    app = Application.builder().token(TOKEN).post_init(on_startup).build()
    for command, handler in [('start', start), ('status', status), ('meciuri', meciuri),
                             ('cote', cote), ('dubla', dubla), ('analiza', analiza),
                             ('istoric', istoric), ('statistici', statistici), ('verifica', verifica)]:
        app.add_handler(CommandHandler(command, handler))
    log.info('Odds Copilot 2.3 started')
    app.run_polling()


if __name__ == '__main__':
    main()
