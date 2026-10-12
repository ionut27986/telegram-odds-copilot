import asyncio
import logging
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


def analyze():
    """Conservative descriptive screen; no predictive AI or proven betting edge."""
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
            home = [g for g in recent_games(h['id'], cache) if g['venue'] == 'home'][:8]
            away = [g for g in recent_games(a['id'], cache) if g['venue'] == 'away'][:8]
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning('Unavailable analysis data: %s', exc)
            continue
        if len(home) < 5 or len(away) < 5:
            continue
        qualified += 1
        for entry in prices:
            for bookmaker in entry.get('bookmakers', []):
                name = bookmaker.get('name', '?')
                for bet in bookmaker.get('bets', []):
                    for outcome in bet.get('values', []):
                        label = str(outcome.get('value', '')).strip()
                        market = exact_goal_market(bet.get('id'), label)
                        if not market:
                            continue
                        try:
                            odd = float(outcome.get('odd'))
                        except (TypeError, ValueError):
                            continue
                        if not 1.25 <= odd <= 1.85:
                            continue
                        hh, hn = sample_frequency(home, market)
                        ah, an = sample_frequency(away, market)
                        # Descriptive consistency filter; not a modelled match probability.
                        home_rate, away_rate = hh / hn, ah / an
                        if min(home_rate, away_rate) < 0.60:
                            continue
                        choices.append({
                            'fixture': game['fixture']['id'], 'kick': kick,
                            'home': h.get('name', '?'), 'away': a.get('name', '?'),
                            'bookmaker': name, 'market': bet.get('name', '?'),
                            'selection': label, 'odd': odd,
                            'home_hits': hh, 'home_n': hn, 'away_hits': ah, 'away_n': an,
                            'consistency': min(home_rate, away_rate),
                        })
    # One candidate per fixture/market/selection/bookmaker. Distinct fixtures and same bookmaker.
    unique = {}
    for c in choices:
        unique[(c['fixture'], c['market'], c['selection'], c['bookmaker'])] = c
    choices = sorted(unique.values(), key=lambda c: (-c['consistency'], -c['odd']))[:150]
    best, rank = None, float('-inf')
    for x, y in combinations(choices, 2):
        if x['fixture'] == y['fixture'] or x['bookmaker'] != y['bookmaker']:
            continue
        combined = x['odd'] * y['odd']
        if not 1.90 <= combined <= 2.10:
            continue
        score = min(x['consistency'], y['consistency']) - abs(combined - 2) * 0.2
        if score > rank:
            best, rank = (x, y, combined), score
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
    await update.message.reply_text('🤖 Odds Copilot online!\n/status - status\n/meciuri - upcoming fixtures\n/cote - sample odds\n/dubla - mathematical 1X2 test\n/analiza - goal/BTTS statistical test')


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('✅ Odds Copilot funcționează.')


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


async def analiza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔎 Checking home/away goal statistics and bookmaker odds...')
    try:
        best, checked, qualified, choices = await asyncio.to_thread(analyze)
        if not best:
            await update.message.reply_text(f'📊 Checked {checked} fixtures; {qualified} had enough home/away data; {choices} odds passed the descriptive screen. No double near 2.00. No picks forced.')
            return
        x, y, total = best
        lines = ['🧪 Odds Copilot — home/away statistical screen (NOT AI prediction)']
        for i, c in enumerate((x, y), 1):
            lines.append(f"{i}. {c['home']} vs {c['away']} — {c['kick'].astimezone(LOCAL):%H:%M}\n{c['market']}: {c['selection']} @ {c['odd']:.2f}\nBookmaker: {c['bookmaker']}\nHome team, at home: {c['home_hits']}/{c['home_n']} matches\nAway team, away: {c['away_hits']}/{c['away_n']} matches\nRaw implied probability: {1/c['odd']:.1%}")
        lines.append(f'Combined odds: {total:.2f}')
        lines.append('⚠️ Experimental historical-frequency filter only, NOT calibrated probabilities, AI prediction, or evidence of betting value. Quarter/integer Asian totals excluded. No corner/card analysis or automatic publishing. Odds may change.')
        await update.message.reply_text('\n\n'.join(lines))
    except Exception:
        log.exception('Analysis failed')
        await update.message.reply_text('❌ Analysis error. Check Railway logs.')


def main():
    if not TOKEN or not KEY:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or API_FOOTBALL_KEY in Railway')
    app = Application.builder().token(TOKEN).build()
    for command, handler in [('start', start), ('status', status), ('meciuri', meciuri),
                             ('cote', cote), ('dubla', dubla), ('analiza', analiza)]:
        app.add_handler(CommandHandler(command, handler))
    log.info('Odds Copilot started')
    app.run_polling()


if __name__ == '__main__':
    main()
