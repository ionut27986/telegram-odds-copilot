
import os
import logging
import asyncio
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
    days = sorted({
        now.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d")
    })
    matches = []

    for day in days:
        data = api_get(
            "fixtures",
            {"date": day, "timezone": "UTC"}
        )
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Odds Copilot is online!\n\n"
        "Commands:\n"
        "/status - bot status\n"
        "/meciuri - upcoming matches\n"
        "/cote - test available odds"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activ\n"
        "📡 Railway: online\n"
        "⚽ Odds Copilot: pregătit"
    )


async def meciuri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text(
            "❌ API_FOOTBALL_KEY nu este configurată."
        )
        return

    await update.message.reply_text(
        "🔎 Caut meciuri în următoarele 2 ore..."
    )

    try:
        matches = await asyncio.to_thread(fetch_fixtures, 2)
    except Exception:
        logger.exception("Eroare la preluarea meciurilor")
        await update.message.reply_text(
            "❌ Eroare API. Verifică Railway Logs."
        )
        return

    if not matches:
        await update.message.reply_text(
            "⚽ Nu am găsit meciuri în următoarele 2 ore."
        )
        return

    lines = [
        f"⚽ Meciuri în următoarele 2 ore: {len(matches)}"
    ]

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


def fetch_odds_sample():

def fetch_odds_sample():
    matches = fetch_fixtures(2)

    if not matches:
        return [], 0

    # Testăm maximum 3 meciuri, pentru a economisi
    # cererile disponibile în abonamentul API.
    results = []
    checked = 0

    for kickoff, match in matches[:3]:
        fixture = match.get("fixture", {})
        league = match.get("league", {})
        teams = match.get("teams", {})

        fixture_id = fixture.get("id")
        league_id = league.get("id")

        if not fixture_id or not league_id:
            continue

        checked += 1

        data = api_get(
            "odds",
            {
                "fixture": fixture_id,
                "league": league_id,
                "date": kickoff.strftime("%Y-%m-%d"),
            },
        )

        for item in data.get("response", []):
            bookmakers = item.get("bookmakers", [])

            if not bookmakers:
                continue

            book = bookmakers[0]
            bets = book.get("bets", [])

            if not bets:
                continue

            bet = next(
                (b for b in bets if b.get("id") == 1),
                bets[0]
            )

            results.append({
                "fixture_id": fixture_id,
                "league": league.get("name", "?"),
                "bookmaker": book.get("name", "?"),
                "market": bet.get("name", "?"),
                "outcomes": [
                    (o.get("value", "?"), o.get("odd", "?"))
                    for o in bet.get("values", [])[:3]
                ],
            })

            break

    return results, checked



async def cote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text(
            "❌ API_FOOTBALL_KEY nu este configurată."
        )
        return

    await update.message.reply_text(
        "🔎 Checking available pre-match odds..."
    )

    try:
        samples, count = await asyncio.to_thread(
            fetch_odds_sample
        )
    except ValueError as exc:
        logger.warning("Odds API error: %s", exc)
        await update.message.reply_text(
            "⚠️ API-Football nu a permis accesul la cote.\n"
            "Este posibil ca planul Free să nu includă "
            "endpoint-ul /odds.\n"
            "Verifică Railway Logs pentru detalii."
        )
        return
    except Exception:
        logger.exception("Eroare la preluarea cotelor")
        await update.message.reply_text(
            "❌ Eroare la preluarea cotelor. "
            "Verifică Railway Logs."
        )
        return

    if not samples:
        await update.message.reply_text(
            "⚠️ API-ul a răspuns, dar nu a furnizat "
            "exemple de cote.\n"
            f"Înregistrări în răspuns: {count}."
        )
        return

    lines = [
        "📊 Pre-match odds - API test",
        "Examples only, not recommendations."
    ]

    for item in samples:
        lines.append(
            f"\n⚽ Fixture ID: {item['fixture_id']}"
            f"\n🏆 {item['league']}"
            f"\n🏦 {item['bookmaker']}"
            f"\n📈 {item['market']}"
        )

        for name, odd in item["outcomes"]:
            lines.append(f"• {name}: {odd}")

    lines.append(
        "\nℹ️ Test de acces la cote, nu selecții analizate."
    )

    await update.message.reply_text("\n".join(lines))


def main():
    if not TOKEN:
        raise RuntimeError(
            "Variabila TELEGRAM_BOT_TOKEN nu este configurată."
        )

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("meciuri", meciuri))
    application.add_handler(CommandHandler("cote", cote))

    logger.info("Odds Copilot pornit...")
    application.run_polling()


if __name__ == "__main__":
    main()
