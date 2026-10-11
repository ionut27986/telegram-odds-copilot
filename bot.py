
import os
import logging
import asyncio
import requests

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
API_URL = "https://v3.football.api-sports.io"
RO_TIME = ZoneInfo("Europe/Bucharest")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Odds Copilot este online!\n\n"
        "Botul este conectat și funcționează.\n\n"
        "Comenzi disponibile:\n"
        "/status - verifică botul\n"
        "/meciuri - meciuri în următoarele 2 ore"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activ\n"
        "📡 Railway: online\n"
        "⚽ Odds Copilot: pregătit"
    )


def fetch_fixtures():
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=2)

    dates = sorted({
        now.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d")
    })

    headers = {"x-apisports-key": API_KEY}
    fixtures = []

    for day in dates:
        response = requests.get(
            f"{API_URL}/fixtures",
            headers=headers,
            params={"date": day, "timezone": "UTC"},
            timeout=20
        )
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            raise ValueError(str(data["errors"]))

        fixtures.extend(data.get("response", []))

    results = []

    for match in fixtures:
        fixture = match.get("fixture", {})
        date_text = fixture.get("date")

        if not date_text:
            continue

        kickoff = datetime.fromisoformat(
            date_text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        if not now <= kickoff <= end:
            continue

        teams = match.get("teams", {})
        league = match.get("league", {})

        results.append({
            "time": kickoff,
            "home": teams.get("home", {}).get("name", "?"),
            "away": teams.get("away", {}).get("name", "?"),
            "league": league.get("name", "?")
        })

    return sorted(results, key=lambda m: m["time"])


async def meciuri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text(
            "❌ API_FOOTBALL_KEY nu este configurată."
        )
        return

    await update.message.reply_text(
        "🔎 Caut meciurile din următoarele 2 ore..."
    )

    try:
        matches = await asyncio.to_thread(fetch_fixtures)
    except Exception:
        logger.exception("Eroare la API-Football")
        await update.message.reply_text(
            "❌ Nu am putut prelua meciurile. "
            "Verifică logurile Railway și accesul API."
        )
        return

    if not matches:
        await update.message.reply_text(
            "⚽ Nu am găsit meciuri în următoarele 2 ore "
            "în datele disponibile prin abonamentul API."
        )
        return

    lines = [
        f"⚽ Meciuri în următoarele 2 ore: {len(matches)}\n"
    ]

    for match in matches[:30]:
        local_time = match["time"].astimezone(RO_TIME)
        lines.append(
            f"\n🕒 {local_time:%H:%M} - "
            f"{match['home']} vs {match['away']}\n"
            f"🏆 {match['league']}"
        )

    if len(matches) > 30:
        lines.append("\nSe afișează primele 30 de meciuri.")

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

    print("Odds Copilot pornit...")
    application.run_polling()


if __name__ == "__main__":
    main()
