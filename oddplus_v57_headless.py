
"""OddPlus v5.7 - headless scanner with API synchronization."""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from bot_v57 import (
    MIN_LEAD_MINUTES,
    V55_SCAN_INTERVAL_HOURS,
    v57_db,
    v57_scan,
    v57_settle,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oddplus-v57")

SYNC_URL = os.getenv("ODDPLUS_SYNC_URL", "").strip()
SYNC_TOKEN = os.getenv("ODDPLUS_INTERNAL_TOKEN", "").strip()

SYNC_INTERVAL_SECONDS = 300


def publish_to_ledger():
    now = datetime.now(timezone.utc)

    with v57_db() as db:
        rows = db.execute(
            """
            SELECT id, kickoff
            FROM direct_picks_57
            WHERE published_at IS NULL
            """
        ).fetchall()

        eligible = []

        for pick_id, kickoff in rows:
            kick = datetime.fromisoformat(kickoff)

            if kick.tzinfo is None:
                kick = kick.replace(tzinfo=timezone.utc)

            if kick > now + timedelta(minutes=MIN_LEAD_MINUTES):
                eligible.append((now.isoformat(), pick_id))

        db.executemany(
            """
            UPDATE direct_picks_57
            SET published_at = ?
            WHERE id = ? AND published_at IS NULL
            """,
            eligible,
        )

    return len(eligible)


def load_published_picks():
    with v57_db() as db:
        db.row_factory = __import__("sqlite3").Row

        rows = db.execute(
            """
            SELECT *
            FROM direct_picks_57
            WHERE published_at IS NOT NULL
            ORDER BY published_at DESC
            LIMIT 100
            """
        ).fetchall()

        picks = []

        for row in rows:
            pick = dict(row)

            if (
                pick.get("id") is None
                or pick.get("fixture_id") is None
                or not pick.get("published_at")
            ):
                continue

            picks.append({
                "id": pick["id"],
                "fixture_id": pick["fixture_id"],
                "published_at": pick["published_at"],
                "kickoff": pick.get("kickoff"),
                "league": pick.get("league"),
                "home": pick.get("home"),
                "away": pick.get("away"),
                "market": pick.get("market"),
                "selection": pick.get("selection"),
                "odd": pick.get("odd"),
                "estimated_ev": pick.get("estimated_ev"),
                "status": pick.get("status"),
                "home_goals": pick.get("home_goals"),
                "away_goals": pick.get("away_goals"),
            })

    return picks


def sync_to_api():
    if not SYNC_URL or not SYNC_TOKEN:
        log.error("OddPlus API synchronization is not configured")
        return False

    picks = load_published_picks()

    if not picks:
        log.info("No published v5.7 picks to synchronize")
        return True

    payload = json.dumps({
        "version": "5.7",
        "picks": picks,
    }).encode("utf-8")

    request = Request(
        SYNC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": SYNC_TOKEN,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)

        if result.get("status") != "ok":
            log.error("Unexpected API sync response: %s", result)
            return False

        log.info(
            "OddPlus API synchronized: %s picks",
            result.get("received", 0),
        )
        return True

    except Exception:
        log.exception("OddPlus API synchronization failed")
        return False


def main():
    log.info("OddPlus v5.7 headless starting")

    with v57_db():
        pass

    scan_interval = max(
        1,
        float(V55_SCAN_INTERVAL_HOURS),
    ) * 3600

    next_scan = time.monotonic() + 30
    next_settle = time.monotonic() + 1800
    next_sync = time.monotonic() + 10

    while True:
        now = time.monotonic()

        if now >= next_scan:
            try:
                log.info("Scan: %s", v57_scan())
                log.info(
                    "Published to ledger: %s",
                    publish_to_ledger(),
                )
            except Exception:
                log.exception("v5.7 scan failed")

            next_scan = time.monotonic() + scan_interval
            next_sync = time.monotonic()

        if now >= next_settle:
            try:
                v57_settle()
                log.info("v5.7 settlement complete")
            except Exception:
                log.exception("v5.7 settlement failed")

            next_settle = time.monotonic() + 1800

        if now >= next_sync:
            try:
                sync_to_api()
            except Exception:
                log.exception("Unexpected synchronization error")

            next_sync = time.monotonic() + SYNC_INTERVAL_SECONDS

        time.sleep(5)


if __name__ == "__main__":
    main()
