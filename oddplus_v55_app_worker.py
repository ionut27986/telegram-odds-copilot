"""OddPlus v5.5 app-only worker. Separate ledger, no Telegram publishing."""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from bot import (
    MIN_LEAD_MINUTES, V55_SCAN_INTERVAL_HOURS,
    v53_db, v55_db, v55_scan, v55_settle,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oddplus-v55-app")
SYNC_URL = os.getenv("ODDPLUS_SYNC_URL", "").strip()
SYNC_TOKEN = os.getenv("ODDPLUS_INTERNAL_TOKEN", "").strip()


def publish_to_ledger():
    """Publish only future picks; expired unposted picks stay unpublished."""
    now = datetime.now(timezone.utc)
    with v55_db() as db:
        rows = db.execute(
            "SELECT id,kickoff FROM direct_picks_55 WHERE published_at IS NULL"
        ).fetchall()
        eligible = []
        for pick_id, kickoff in rows:
            kick = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if kick.tzinfo is None:
                kick = kick.replace(tzinfo=timezone.utc)
            if kick > now + timedelta(minutes=MIN_LEAD_MINUTES):
                eligible.append((now.isoformat(), pick_id))
        db.executemany(
            "UPDATE direct_picks_55 SET published_at=? "
            "WHERE id=? AND published_at IS NULL", eligible
        )
    return len(eligible)


def sync_to_api():
    if not SYNC_URL or not SYNC_TOKEN:
        log.warning("Sync is not configured")
        return
    with v55_db() as db:
        db.row_factory = __import__("sqlite3").Row
        rows = db.execute(
            "SELECT id,fixture_id,published_at,kickoff,league,home,away,"
            "bookmaker,market,selection,odd,estimated,implied,estimated_ev,"
            "status,home_goals,away_goals FROM direct_picks_55 "
            "WHERE published_at IS NOT NULL "
            "ORDER BY published_at DESC LIMIT 100"
        ).fetchall()
    if not rows:
        return
    payload = json.dumps({
        "version": "5.5", "picks": [dict(row) for row in rows]
    }).encode("utf-8")
    request = Request(
        SYNC_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Internal-Token": SYNC_TOKEN},
    )
    with urlopen(request, timeout=20) as response:
        result = json.load(response)
    if result.get("status") != "ok":
        raise RuntimeError("Unexpected sync response")
    log.info("Synced %s v5.5 picks", result.get("received"))


def main():
    if not os.getenv("API_FOOTBALL_KEY"):
        raise RuntimeError("API_FOOTBALL_KEY missing")
    # v5.5 reads the v5.3 fixture ledger to avoid duplicates; initialize it empty.
    with v53_db():
        pass
    with v55_db():
        pass
    next_scan = time.monotonic() + 30
    next_settle = time.monotonic() + 1800
    next_sync = time.monotonic() + 10
    while True:
        now = time.monotonic()
        if now >= next_scan:
            try:
                log.info("%s", v55_scan())
                log.info("Published %s", publish_to_ledger())
            except Exception:
                log.exception("v5.5 scan failed")
            next_scan = time.monotonic() + max(1, V55_SCAN_INTERVAL_HOURS) * 3600
            next_sync = time.monotonic()
        if now >= next_settle:
            try:
                log.info("Settled %s", v55_settle())
            except Exception:
                log.exception("v5.5 settlement failed")
            next_settle = time.monotonic() + 1800
            next_sync = time.monotonic()
        if now >= next_sync:
            try:
                sync_to_api()
            except Exception:
                log.exception("v5.5 sync failed")
            next_sync = time.monotonic() + 300
        time.sleep(5)


if __name__ == "__main__":
    main()
