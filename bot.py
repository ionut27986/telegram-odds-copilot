import asyncio
import logging
import os
from itertools import combinations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
API_URL = "https://v3.football.api-sports.io"
RO_TIME = ZoneInfo("Europe/Bucharest")


def api_get(endpoint, params):
    response = requests.get(
        f"{API_URL}/{endpoint}",
        headers={"x-apisports-key": API_KEY},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise ValueError(str(data["errors"]))
    return data


def fetch_fixtures(hours=2):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    dates = sorted({now.date().isoformat(), end.date().isoformat()})
    matches = []

    for day in dates:
        data = api_get("fixtures", {"date": day, "timezone": "UTC"})
        for match in data.get("response", []):
            date_text = match.get("fixture", {}).get("date")
            if not date_text:
                continue
            kickoff = datetime.fromisoformat(
                date_text.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if now <= kickoff <= end:
                matches.append((kickoff, match))

    return sorted(matches, key=lambda item: item[0])


def fetch_odds_sample():
    matches = fetch_fixtures(hours=2)
    if not matches:
        return [], 0, "Nu sunt meciuri în următoarele 2 ore."

    checked = 0
    results = []
    for kickoff, match in matches[:5]:
        fixture = match.get("fixture", {})
        league = match.get("league", {})
        teams = match.get("teams", {})
        season = league.get("season")
        fixture_id = fixture.get("id")
        league_id = league.get("id")
        if not fixture_id or not league_id or season is None:
            continue

        checked += 1
        data = api_get(
            "odds",
            {
                "fixture": fixture_id,
                "league": league_id,
                "season": season,
                "date": kickoff.date().isoformat(),
            },
        )
        for item in data.get("response", []):
            for bookmaker in item.get("bookmakers", [])[:1]:
                bets = bookmaker.get("bets", [])
                if not bets:
                    continue
                market = next((bet for bet in bets if bet.get("id") == 1), bets[0])
                values = market.get("values", [])[:3]
                if not values:
                    continue
                results.append({
                    "home": teams.get("home", {}).get("name", "?"),
                    "away": teams.get("away", {}).get("name", "?"),
                    "time": kickoff.astimezone(RO_TIME).strftime("%H:%M"),
                    "bookmaker": bookmaker.get("name", "?"),
                    "market": market.get("name", "?"),
                    "values": values,
                })
                break
    return results, checked, ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Odds Copilot este online!\n\n"
        "Comenzi:\n"
        "/status - stare bot\n"
        "/meciuri - meciuri în următoarele 2 ore\n"
        "/cote - test cote disponibile\n"
        "/dubla - caută o dublă de test (~2.00)\n"
        "/analiza - test statistici goluri și BTTS"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Odds Copilot funcționează.")


async def meciuri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text("❌ Lipsește API_FOOTBALL_KEY în Railway.")
        return
    await update.message.reply_text("🔎 Caut meciuri în următoarele 2 ore...")
    try:
        matches = await asyncio.to_thread(fetch_fixtures, 2)
    except Exception as exc:
        logger.exception("Eroare la meciuri")
        await update.message.reply_text(f"❌ Eroare API: {str(exc)[:500]}")
        return

    if not matches:
        await update.message.reply_text("⚽ Nu sunt meciuri disponibile în următoarele 2 ore.")
        return

    lines = [f"⚽ Meciuri în următoarele 2 ore: {len(matches)}"]
    for kickoff, match in matches[:30]:
        teams = match.get("teams", {})
        league = match.get("league", {})
        local = kickoff.astimezone(RO_TIME)
        lines.append(
            f"\n🕒 {local:%H:%M} - "
            f"{teams.get('home', {}).get('name', '?')} vs "
            f"{teams.get('away', {}).get('name', '?')}\n"
            f"🏆 {league.get('name', '?')}"
        )
    if len(matches) > 30:
        lines.append("\nSe afișează primele 30 de meciuri.")
    await update.message.reply_text("\n".join(lines))


async def cote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text("❌ Lipsește API_FOOTBALL_KEY în Railway.")
        return
    await update.message.reply_text("🔎 Verific disponibilitatea cotelor...")
    try:
        results, checked, note = await asyncio.to_thread(fetch_odds_sample)
    except Exception as exc:
        logger.exception("Eroare la cote")
        await update.message.reply_text(
            f"⚠️ API-Football a răspuns cu o eroare:\n{str(exc)[:700]}"
        )
        return

    if not results:
        await update.message.reply_text(
            f"⚠️ Nu am primit cote pentru cele {checked} meciuri verificate.\n{note}"
        )
        return

    lines = ["📊 Cote disponibile (test, nu recomandări):"]
    for item in results[:5]:
        lines.append(
            f"\n⚽ {item['time']} - {item['home']} vs {item['away']}\n"
            f"🏦 {item['bookmaker']} | {item['market']}"
        )
        for value in item["values"]:
            lines.append(f"• {value.get('value', '?')}: {value.get('odd', '?')}")
    await update.message.reply_text("\n".join(lines))



def find_double():
    """Demonstration only: finds two selections, not a statistical forecast."""
    matches = fetch_fixtures(hours=4)
    candidates = []
    checked = 0
    for kickoff, match in matches[:20]:
        fixture = match.get("fixture", {})
        league = match.get("league", {})
        teams = match.get("teams", {})
        if not fixture.get("id") or not league.get("id") or league.get("season") is None:
            continue
        checked += 1
        try:
            data = api_get("odds", {
                "fixture": fixture["id"],
                "league": league["id"],
                "season": league["season"],
                "date": kickoff.date().isoformat(),
            })
        except (ValueError, requests.RequestException) as exc:
            logger.warning("Odds unavailable for %s: %s", fixture["id"], exc)
            continue
        for entry in data.get("response", []):
            for bookmaker in entry.get("bookmakers", []):
                # Compare both selections at the same bookmaker.
                for bet in bookmaker.get("bets", []):
                    if bet.get("id") != 1:  # Match winner (1X2)
                        continue
                    for outcome in bet.get("values", []):
                        try:
                            odd = float(outcome.get("odd", ""))
                        except (ValueError, TypeError):
                            continue
                        if not 1.25 <= odd <= 1.75:
                            continue
                        candidates.append({
                            "fixture_id": fixture["id"],
                            "time": kickoff,
                            "home": teams.get("home", {}).get("name", "?"),
                            "away": teams.get("away", {}).get("name", "?"),
                            "bookmaker": bookmaker.get("name", "?"),
                            "market": bet.get("name", "Match Winner"),
                            "selection": outcome.get("value", "?"),
                            "odd": odd,
                        })
    best = None
    best_score = float("inf")
    for a, b in combinations(candidates, 2):
        if a["fixture_id"] == b["fixture_id"]:
            continue
        if a["bookmaker"] != b["bookmaker"]:
            continue
        combined = a["odd"] * b["odd"]
        if not 1.90 <= combined <= 2.10:
            continue
        score = abs(combined - 2.00)
        if score < best_score:
            best = (a, b, combined)
            best_score = score
    return best, checked


async def dubla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text("❌ Lipsește API_FOOTBALL_KEY în Railway.")
        return
    await update.message.reply_text("🔎 Caut o dublă demonstrativă pentru următoarele 4 ore...")
    try:
        best, checked = await asyncio.to_thread(find_double)
    except Exception:
        logger.exception("Eroare la dubla")
        await update.message.reply_text("❌ Eroare la verificare. Consultă Railway Logs.")
        return
    if best is None:
        await update.message.reply_text(
            f"Nu am găsit o dublă între 1.90 și 2.10 în {checked} meciuri verificate. "
            "Nu forțăm o combinație."
        )
        return
    a, b, combined = best
    lines = ["🧪 Odds Copilot — Dublă demonstrativă", ""]
    for index, item in enumerate((a, b), start=1):
        local = item["time"].astimezone(RO_TIME)
        lines.extend([
            f"{index}. {item['home']} vs {item['away']} — {local:%H:%M}",
            f"{item['market']}: {item['selection']} @ {item['odd']:.2f}",
            f"Probabilitate implicită brută: {100/item['odd']:.1f}%",
            "",
        ])
    lines.extend([
        f"🏦 Casă: {a['bookmaker']}",
        f"📊 Cotă combinată: {combined:.2f}",
        "",
        "⚠️ Selecție strict matematică, NU analiză AI sau recomandare.",
        "Probabilitățile implicite includ marja casei; nu reprezintă șanse estimate independent.",
        "Cotele se pot modifica înainte de începerea meciurilor.",
    ])
    await update.message.reply_text("\n".join(lines))



def analyze_candidates():
    """Experimental goal/BTTS frequency screen; not a calibrated forecast."""
    matches = fetch_fixtures(hours=6)
    team_cache = {}
    candidates = []
    checked = 0

    def history(team_id):
        if team_id not in team_cache:
            data = api_get("fixtures", {"team": team_id, "last": 8})
            games = []
            for item in data.get("response", []):
                goals = item.get("goals", {})
                sides = item.get("teams", {})
                h, a = sides.get("home", {}), sides.get("away", {})
                hg, ag = goals.get("home"), goals.get("away")
                if hg is None or ag is None:
                    continue
                if h.get("id") == team_id:
                    games.append((hg, ag))
                elif a.get("id") == team_id:
                    games.append((ag, hg))
            team_cache[team_id] = games
        return team_cache[team_id]

    for kickoff, match in matches[:10]:
        fixture = match.get("fixture", {})
        league = match.get("league", {})
        teams = match.get("teams", {})
        home_id = teams.get("home", {}).get("id")
        away_id = teams.get("away", {}).get("id")
        if not fixture.get("id") or not league.get("id") or league.get("season") is None:
            continue
        if not home_id or not away_id:
            continue
        checked += 1
        try:
            odds = api_get("odds", {
                "fixture": fixture["id"], "league": league["id"],
                "season": league["season"], "date": kickoff.date().isoformat(),
            })
            hg, ag = history(home_id), history(away_id)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Statistics unavailable for %s: %s", fixture["id"], exc)
            continue
        if len(hg) < 5 or len(ag) < 5:
            continue
        observations = hg + ag
        n = len(observations)
        for entry in odds.get("response", []):
            for bookmaker in entry.get("bookmakers", []):
                for market in bookmaker.get("bets", []):
                    market_id = market.get("id")
                    if market_id not in (5, 8):
                        continue
                    for outcome in market.get("values", []):
                        label = str(outcome.get("value", "")).strip()
                        try:
                            odd = float(outcome.get("odd"))
                        except (TypeError, ValueError):
                            continue
                        if not 1.25 <= odd <= 1.85:
                            continue
                        hits = None
                        if market_id == 8 and label.lower() in ("yes", "no"):
                            yes = label.lower() == "yes"
                            hits = sum(((sc > 0 and co > 0) == yes) for sc, co in observations)
                        elif market_id == 5:
                            parts = label.split()
                            if len(parts) == 2 and parts[0].lower() in ("over", "under"):
                                try:
                                    threshold = float(parts[1])
                                except ValueError:
                                    continue
                                over = parts[0].lower() == "over"
                                hits = sum(((sc + co > threshold) == over) for sc, co in observations)
                        if hits is None:
                            continue
                        # Shrink small samples toward 50%; heuristic only.
                        frequency = (hits + 3) / (n + 6)
                        edge_indicator = frequency - (1 / odd)
                        if edge_indicator < 0.03:
                            continue
                        candidates.append({
                            "fixture_id": fixture["id"], "time": kickoff,
                            "home": teams["home"].get("name", "?"),
                            "away": teams["away"].get("name", "?"),
                            "bookmaker": bookmaker.get("name", "?"),
                            "market": market.get("name", "?"),
                            "selection": label, "odd": odd,
                            "frequency": frequency, "sample": n,
                            "edge_indicator": edge_indicator,
                        })

    candidates.sort(key=lambda x: x["edge_indicator"], reverse=True)
    best, best_score = None, float("-inf")
    for a, b in combinations(candidates[:120], 2):
        if a["fixture_id"] == b["fixture_id"] or a["bookmaker"] != b["bookmaker"]:
            continue
        combined = a["odd"] * b["odd"]
        if not 1.90 <= combined <= 2.10:
            continue
        score = a["edge_indicator"] + b["edge_indicator"] - abs(combined - 2.0) / 10
        if score > best_score:
            best, best_score = (a, b, combined), score
    return best, checked, len(candidates)


async def analiza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text("❌ Lipsește API_FOOTBALL_KEY în Railway.")
        return
    await update.message.reply_text("🔎 Checking recent goal statistics and bookmaker odds...")
    try:
        best, checked, count = await asyncio.to_thread(analyze_candidates)
    except Exception:
        logger.exception("Analysis failed")
        await update.message.reply_text("❌ Analysis failed. Check Railway Logs.")
        return
    if best is None:
        await update.message.reply_text(
            f"📊 Checked {checked} fixtures. {count} candidate odds passed "
            "the preliminary screen, but no double near 2.00 was found. "
            "No picks forced."
        )
        return
    a, b, combined = best
    lines = ["🧪 Odds Copilot — Statistical test (NOT AI prediction)", ""]
    for i, item in enumerate((a, b), 1):
        local = item["time"].astimezone(RO_TIME)
        lines.extend([
            f"{i}. {item['home']} vs {item['away']} — {local:%H:%M}",
            f"{item['market']}: {item['selection']} @ {item['odd']:.2f}",
            f"Bookmaker: {item['bookmaker']}",
            f"Smoothed recent frequency: {item['frequency']:.1%} ({item['sample']} observations)",
            f"Raw implied probability: {1/item['odd']:.1%}", "",
        ])
    lines.extend([
        f"Combined odds: {combined:.2f}", "",
        "⚠️ Experimental screening only. Recent frequencies are not calibrated "
        "probabilities or evidence of value. No corner/card or injury analysis yet. "
        "No automatic publishing. Odds can change.",
    ])
    await update.message.reply_text("\n".join(lines))

def main():
    if not TOKEN:
        raise RuntimeError("Lipsește TELEGRAM_BOT_TOKEN în Railway.")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("meciuri", meciuri))
    application.add_handler(CommandHandler("cote", cote))
    application.add_handler(CommandHandler("dubla", dubla))
    application.add_handler(CommandHandler("analiza", analiza))
    logger.info("Odds Copilot pornit")
    application.run_polling()


if __name__ == "__main__":
    main()
