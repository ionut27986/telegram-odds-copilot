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


def settlement_summary():
    """Operational counts only; never alter the independent app ledger."""
    now = datetime.now(timezone.utc)
    with v55_db() as db:
        rows = db.execute(
            "SELECT status,kickoff FROM direct_picks_55 "
            "WHERE published_at IS NOT NULL"
        ).fetchall()
    pending = due = finished = 0
    for status, kickoff in rows:
        if status in ("won", "lost", "void"):
            finished += 1
        elif status == "pending":
            pending += 1
            try:
                kick = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                if kick.tzinfo is None:
                    kick = kick.replace(tzinfo=timezone.utc)
                if kick + timedelta(hours=2, minutes=15) <= now:
                    due += 1
            except (ValueError, TypeError):
                log.warning("Invalid kickoff in settlement summary")
    return f"Published ledger: {finished} settled, {pending} pending, {due} due for result check"


def sync_telegram_v55():
    """Import published v5.5 picks from Telegram's read-only internal API.

    Do not alter Telegram's database or accept data from other model versions.
    """
    source = os.getenv("ODDPLUS_TELEGRAM_V55_URL",
                       "http://telegram-odds-copilot.railway.internal:8090/internal/picks")
    request = Request(source, headers={"X-Internal-Token": SYNC_TOKEN})
    with urlopen(request, timeout=20) as response:
        data = json.load(response)
    if data.get("version") != "5.5":
        raise RuntimeError("Telegram source is not v5.5")
    picks = data.get("picks", [])
    # Demo-only source must never suppress real v5.5 scanning and settlement.
    # The Telegram internal API currently serves an isolated test ledger.
    real_picks = [p for p in picks
                  if str(p.get("league") or "").strip().lower() != "oddplus demo"]
    if not real_picks:
        log.warning("Telegram v5.5 source has no real published picks; "
                    "using independent v5.5 worker ledger instead")
        return False
    picks = real_picks
    if not SYNC_URL or not SYNC_TOKEN:
        raise RuntimeError("OddPlus sync configuration missing")
    payload = json.dumps({"version": "5.5", "picks": picks}).encode("utf-8")
    target = Request(SYNC_URL, data=payload, method="POST",
                     headers={"Content-Type": "application/json",
                              "X-Internal-Token": SYNC_TOKEN})
    with urlopen(target, timeout=20) as response:
        result = json.load(response)
    if result.get("status") != "ok":
        raise RuntimeError("OddPlus API rejected Telegram sync")
    settled = sum(str(p.get("status", "")).lower() in ("won", "lost") for p in picks)
    log.info("Telegram v5.5 bridge: %s published, %s WIN/LOSS; synced %s",
             len(picks), settled, result.get("received"))
    return True


def main():
    if not os.getenv("API_FOOTBALL_KEY"):
        raise RuntimeError("API_FOOTBALL_KEY missing")
    # v5.5 reads the v5.3 fixture ledger to avoid duplicates; initialize it empty.
    with v53_db():
        pass
    with v55_db():
        pass
    next_scan = time.monotonic() + 30
    telegram_bridge_active = False
    next_bridge = time.monotonic()
    next_settle = time.monotonic() + 1800
    next_sync = time.monotonic() + 10
    while True:
        now = time.monotonic()
        if now >= next_bridge:
            try:
                telegram_bridge_active = sync_telegram_v55()
            except Exception:
                log.exception("Telegram v5.5 bridge failed; existing ledger preserved")
            next_bridge = time.monotonic() + 300
        if not telegram_bridge_active and now >= next_scan:
            try:
                log.info("%s", v55_scan())
                log.info("Published %s", publish_to_ledger())
            except Exception:
                log.exception("v5.5 scan failed")
            next_scan = time.monotonic() + max(1, V55_SCAN_INTERVAL_HOURS) * 3600
            next_sync = time.monotonic()
        if not telegram_bridge_active and now >= next_settle:
            try:
                log.info("Settled %s v5.5 picks", v55_settle())
                log.info("%s", settlement_summary())
            except Exception:
                log.exception("v5.5 settlement failed")
            next_settle = time.monotonic() + 1800
            next_sync = time.monotonic()
        if not telegram_bridge_active and now >= next_sync:
            try:
                sync_to_api()
            except Exception:
                log.exception("v5.5 sync failed")
            next_sync = time.monotonic() + 300
        time.sleep(5)


if __name__ == "__main__":
    main()
