import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Odds Copilot este online!\n\n"
        "Botul este conectat și funcționează."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot activ\n"
        "📡 Railway: online\n"
        "⚽ Odds Copilot: pregătit"
    )


def main():
    if not TOKEN:
        raise RuntimeError(
            "Variabila TELEGRAM_BOT_TOKEN nu este configurată."
        )

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))

    print("Odds Copilot pornit...")
    application.run_polling()


if __name__ == "__main__":
    main()
