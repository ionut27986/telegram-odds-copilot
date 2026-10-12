
import asyncio
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import combinations
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
KEY = os.getenv("API_FOOTBALL_KEY")
URL = "https://v3.football.api-sports.io"
LOCAL = ZoneInfo("Europe/Bucharest")


def api(path, params):
    response = requests.get(
        f"{URL}/{path}",
        headers={"x-apisports-key": KEY},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("errors"):
        raise ValueError(str(result["errors"]))
    return result.get("response", [])


def fixtures(hours=6):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    found = []

    for day in sorted({
        now.date().isoformat(),
        end.date().isoformat(),
    }):
        games = api("fixtures", {
            "date": day,
            "timezone": "UTC",
        })

        for game in games:
            stamp = game.get("fixture", {}).get("date")
            if not stamp:
                continue

            kick = datetime.fromisoformat(
                stamp.replace("Z", "+00:00")
            ).astimezone(timezone.utc)

            if now <= kick <= end:
                found.append((kick, game))

    return sorted(found, key=lambda x: x[0])


def odds_for(kick, game):
    fix = game.get("fixture", {})
    league = game.get("league", {})

    if (
        not fix.get("id")
        or not league.get("id")
        or league.get("season") is None
    ):
        return []

    return api("odds", {
        "fixture": fix["id"],
        "league": league["id"],
        "season": league["season"],
        "date": kick.date().isoformat(),
    })


def recent_games(team_id, cache):
    if team_id in cache:
        return cache[team_id]

    records = []

    games = api("fixtures", {
        "team": team_id,
        "last": 15,
    })

    for game in games:
        sides = game.get("teams", {})
        goals = game.get("goals", {})

        home = sides.get("home", {})
        away = sides.get("away", {})

        gh = goals.get("home")
        ga = goals.get("away")

        if gh is None or ga is None:
            continue

        if home.get("id") == team_id:
            records.append({
                "venue": "home",
                "for": gh,
                "against": ga,
                "total": gh + ga,
            })

        elif away.get("id") == team_id:
            records.append({
                "venue": "away",
                "for": ga,
                "against": gh,
                "total": gh + ga,
            })

    cache[team_id] = records
    return records


def exact_goal_market(market_id, label):
    # Both teams to score.
    if market_id == 8:
        if label.lower() in ("yes", "no"):
            return ("btts", label.lower() == "yes", None)
        return None

    # Total match goals.
    if market_id != 5:
        return None

    match = re.fullmatch(
        r"(Over|Under)\s+(\d+(?:\.\d+)?)",
        label,
        flags=re.I,
    )

    if not match:
        return None

    threshold = float(match.group(2))

    # Only half-goal lines.
    # Integer and quarter lines have special settlement.
    if abs(threshold % 1 - 0.5) > 1e-8:
        return None

    return (
        "goals",
        match.group(1).lower() == "over",
        threshold,
    )


def poisson_cdf(k, lam):
    return sum(
        math.exp(-lam) * lam ** i / math.factorial(i)
        for i in range(k + 1)
    )


def goal_probability(market, home_lambda, away_lambda):
    kind, positive, threshold = market

    if kind == "goals":
        total_lambda = home_lambda + away_lambda
        under = poisson_cdf(
            math.floor(threshold),
            total_lambda,
        )
        return 1 - under if positive else under

    yes = (
        (1 - math.exp(-home_lambda))
        * (1 - math.exp(-away_lambda))
    )

    return yes if positive else 1 - yes


def analyze():
    """
    Experimental Poisson model.
    Not a calibrated prediction or verified betting edge.
    """
    cache = {}
    choices = []
    inspected = 0
    qualified = 0

    for kick, game in fixtures(6)[:12]:
        teams = game.get("teams", {})
        h = teams.get("home", {})
        a = teams.get("away", {})

        if not h.get("id") or not a.get("id"):
            continue

        inspected += 1

        try:
            prices = odds_for(kick, game)

            home = [
                g for g in recent_games(h["id"], cache)
                if g["venue"] == "home"
            ][:10]

            away = [
                g for g in recent_games(a["id"], cache)
                if g["venue"] == "away"
            ][:10]

        except (
            requests.RequestException,
            ValueError,
            KeyError,
        ) as exc:
            log.warning("Analysis unavailable: %s", exc)
            continue

        if len(home) < 5 or len(away) < 5:
            continue

        qualified += 1

        home_scored = (
            sum(g["for"] for g in home) / len(home)
        )
        home_conceded = (
            sum(g["against"] for g in home) / len(home)
        )
        away_scored = (
            sum(g["for"] for g in away) / len(away)
        )
        away_conceded = (
            sum(g["against"] for g in away) / len(away)
        )

        raw_home = (home_scored + away_conceded) / 2
        raw_away = (away_scored + home_conceded) / 2

        # Illustrative regularization.
        # Baselines are not calibrated by league.
        home_lambda = 0.8 * raw_home + 0.2 * 1.40
        away_lambda = 0.8 * raw_away + 0.2 * 1.15

        home_lambda = max(
            0.15, min(home_lambda, 4.0)
        )
        away_lambda = max(
            0.15, min(away_lambda, 4.0)
        )

        for entry in prices:
            for bookmaker in entry.get("bookmakers", []):
                name = bookmaker.get("name", "?")

                for bet in bookmaker.get("bets", []):
                    for outcome in bet.get("values", []):
                        label = str(
                            outcome.get("value", "")
                        ).strip()

                        market = exact_goal_market(
                            bet.get("id"),
                            label,
                        )

                        if market is None:
                            continue

                        try:
                            odd = float(outcome.get("odd"))
                        except (TypeError, ValueError):
                            continue

                        if not math.isfinite(odd):
                            continue

                        if not 1.25 <= odd <= 1.85:
                            continue

                        estimated = goal_probability(
                            market,
                            home_lambda,
                            away_lambda,
                        )

                        implied = 1 / odd
                        difference = estimated - implied

                        # Experimental screening threshold.
                        if difference < 0.05:
                            continue

                        choices.append({
                            "fixture": game["fixture"]["id"],
                            "kick": kick,
                            "home": h.get("name", "?"),
                            "away": a.get("name", "?"),
                            "bookmaker": name,
                            "market": bet.get("name", "?"),
                            "selection": label,
                            "odd": odd,
                            "estimated": estimated,
                            "implied": implied,
                            "difference": difference,
                            "home_lambda": home_lambda,
                            "away_lambda": away_lambda,
                            "home_n": len(home),
                            "away_n": len(away),
                        })

    unique = {}

    for c in choices:
        key = (
            c["fixture"],
            c["market"],
            c["selection"],
            c["bookmaker"],
        )

        if (
            key not in unique
            or c["odd"] > unique[key]["odd"]
        ):
            unique[key] = c

    choices = sorted(
        unique.values(),
        key=lambda c: c["difference"],
        reverse=True,
    )[:150]

    best = None
    best_score = float("-inf")

    for x, y in combinations(choices, 2):
        if x["fixture"] == y["fixture"]:
            continue

        if x["bookmaker"] != y["bookmaker"]:
            continue

        combined = x["odd"] * y["odd"]

        if not 1.90 <= combined <= 2.10:
            continue

        score = (
            x["difference"]
            + y["difference"]
            - 0.1 * abs(combined - 2.0)
        )

        if score > best_score:
            best = (x, y, combined)
            best_score = score

    return best, inspected, qualified, len(choices)


def find_double():
    candidates = []
    inspected = 0

    for kick, game in fixtures(4)[:20]:
        inspected += 1

        try:
            prices = odds_for(kick, game)
        except (
            requests.RequestException,
            ValueError,
            KeyError,
        ) as exc:
            log.warning("No odds: %s", exc)
            continue

        for entry in prices:
            for book in entry.get("bookmakers", []):
                for bet in book.get("bets", []):
                    if bet.get("id") != 1:
                        continue

                    for val in bet.get("values", []):
                        try:
                            odd = float(val.get("odd"))
                        except (TypeError, ValueError):
                            continue

                        if not math.isfinite(odd):
                            continue

                        if 1.25 <= odd <= 1.75:
                            candidates.append({
                                "fixture": game["fixture"]["id"],
                                "kick": kick,
                                "home": game["teams"]["home"]["name"],
                                "away": game["teams"]["away"]["name"],
                                "bookmaker": book.get("name", "?"),
                                "market": bet.get("name", "?"),
                                "selection": val.get("value", "?"),
                                "odd": odd,
                            })

    best = None
    distance = float("inf")

    for x, y in combinations(candidates, 2):
        if x["fixture"] == y["fixture"]:
            continue

        if x["bookmaker"] != y["bookmaker"]:
            continue

        combined = x["odd"] * y["odd"]

        if (
            1.90 <= combined <= 2.10
            and abs(combined - 2) < distance
        ):
            best = (x, y, combined)
            distance = abs(combined - 2)

    return best, inspected


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🤖 Odds Copilot 2.2 online!\n\n"
        "/status - bot status\n"
        "/meciuri - upcoming fixtures\n"
        "/cote - bookmaker odds\n"
        "/dubla - mathematical 1X2 test\n"
        "/analiza - experimental Poisson analysis"
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "✅ Odds Copilot 2.2 funcționează."
    )


async def meciuri(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔎 Checking fixtures..."
    )

    try:
        games = await asyncio.to_thread(fixtures, 2)

        lines = [
            f"⚽ Fixtures in next 2 hours: {len(games)}"
        ]

        for kick, game in games[:30]:
            home = game["teams"]["home"]["name"]
            away = game["teams"]["away"]["name"]

            lines.append(
                f"{kick.astimezone(LOCAL):%H:%M} "
                f"{home} vs {away}"
            )

        await update.message.reply_text(
            "\n".join(lines)[:4000]
        )

    except Exception:
        log.exception("Fixtures failed")
        await update.message.reply_text(
            "❌ API error. Check Railway logs."
        )


async def cote(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔎 Checking bookmaker odds..."
    )

    try:
        games = await asyncio.to_thread(fixtures, 2)
        lines = ["📊 Sample odds (not recommendations):"]

        for kick, game in games[:5]:
            prices = await asyncio.to_thread(
                odds_for, kick, game
            )

            for entry in prices:
                for book in entry.get("bookmakers", [])[:1]:
                    bet = next(
                        (
                            b for b in book.get("bets", [])
                            if b.get("id") == 1
                        ),
                        None,
                    )

                    if not bet:
                        continue

                    values = ", ".join(
                        f"{v.get('value')}: {v.get('odd')}"
                        for v in bet.get("values", [])
                    )

                    home = game["teams"]["home"]["name"]
                    away = game["teams"]["away"]["name"]

                    lines.append(
                        f"{kick.astimezone(LOCAL):%H:%M} "
                        f"{home} vs {away}\n"
                        f"{book.get('name')}: {values}"
                    )

        await update.message.reply_text(
            "\n\n".join(lines)[:4000]
        )

    except Exception:
        log.exception("Odds failed")
        await update.message.reply_text(
            "❌ API error. Check Railway logs."
        )


async def dubla(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔎 Searching mathematical double..."
    )

    try:
        best, count = await asyncio.to_thread(
            find_double
        )

        if not best:
            await update.message.reply_text(
                f"No qualifying 1X2 double "
                f"among {count} fixtures."
            )
            return

        x, y, total = best

        lines = [
            "🧪 Mathematical double (not analysis):"
        ]

        for c in (x, y):
            lines.append(
                f"{c['home']} vs {c['away']} — "
                f"{c['kick'].astimezone(LOCAL):%H:%M}\n"
                f"{c['selection']} @ {c['odd']:.2f}"
            )

        lines.append(
            f"Bookmaker: {x['bookmaker']}\n"
            f"Combined: {total:.2f}\n"
            "⚠️ No statistical forecast."
        )

        await update.message.reply_text(
            "\n\n".join(lines)
        )

    except Exception:
        log.exception("Double failed")
        await update.message.reply_text(
            "❌ Error. Check Railway logs."
        )


async def analiza(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔎 Odds Copilot 2.2 — Running Poisson goal model..."
    )

    try:
        best, checked, qualified, count = (
            await asyncio.to_thread(analyze)
        )

        if best is None:
            await update.message.reply_text(
                f"📊 Checked: {checked} fixtures\n"
                f"Sufficient data: {qualified}\n"
                f"Candidates: {count}\n\n"
                "No qualifying double near 2.00. "
                "No selections forced."
            )
            return

        x, y, total = best

        lines = [
            "🧪 Odds Copilot 2.2 — Poisson Model",
            "Experimental statistical analysis",
            "",
        ]

        for i, c in enumerate((x, y), 1):
            lines.extend([
                f"{i}. {c['home']} vs {c['away']}",
                f"🕒 {c['kick'].astimezone(LOCAL):%H:%M}",
                f"📊 {c['market']}: {c['selection']}",
                f"💰 Odds: {c['odd']:.2f}",
                f"🏦 {c['bookmaker']}",
                "",
                f"Model estimate: {c['estimated']:.1%}",
                f"Raw implied probability: {c['implied']:.1%}",
                f"Difference: {c['difference']:+.1%}",
                f"Expected goals: "
                f"{c['home_lambda']:.2f} - "
                f"{c['away_lambda']:.2f}",
                f"Historical sample: "
                f"{c['home_n']} home / "
                f"{c['away_n']} away",
                "",
            ])

        lines.extend([
            f"🎯 Combined odds: {total:.2f}",
            "",
            "⚠️ Experimental model, not calibrated "
            "or independently validated. Estimated "
            "probability differences are not proof "
            "of betting value. No automatic publishing."
        ])

        await update.message.reply_text(
            "\n".join(lines)
        )

    except Exception:
        log.exception("Poisson analysis failed")
        await update.message.reply_text(
            "❌ Analysis error. Check Railway logs."
        )


def main():
    if not TOKEN or not KEY:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or "
            "API_FOOTBALL_KEY in Railway"
        )

    app = Application.builder().token(TOKEN).build()

    for command, handler in [
        ("start", start),
        ("status", status),
        ("meciuri", meciuri),
        ("cote", cote),
        ("dubla", dubla),
        ("analiza", analiza),
    ]:
        app.add_handler(
            CommandHandler(command, handler)
        )

    log.info("Odds Copilot 2.2 started")
    app.run_polling()


if __name__ == "__main__":
    main()
