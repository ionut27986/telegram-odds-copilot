
"""OddPlus v5.7: scanare fotbal fără Telegram polling."""

import logging
import time
from datetime import datetime, timedelta, timezone

from bot_v57 import (
    MIN_LEAD_MINUTES,
    V55_SCAN_INTERVAL_HOURS,
    v57_db,
    v57_scan,
    v57_settle,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oddplus-v57")


def publish_to_ledger():
    """Marchează selecțiile eligibile ca publicate în baza de date."""
    now = datetime.now(timezone.utc)
    with v57_db() as db:
        rows = db.execute(
            "SELECT id, kickoff FROM direct_picks_57 "
            "WHERE published_at IS NULL"
        ).fetchall()

        eligible = []
        for pick_id, kickoff in rows:
            kick = datetime.fromisoformat(kickoff)
            if kick > now + timedelta(minutes=MIN_LEAD_MINUTES):
                eligible.append((now.isoformat(), pick_id))

        db.executemany(
            "UPDATE direct_picks_57 SET published_at=? "
            "WHERE id=? AND published_at IS NULL",
            eligible,
        )

    return len(eligible)


def main():
    log.info("OddPlus v5.7 headless starting")

    with v57_db():
        pass

    scan_interval = max(1, float(V55_SCAN_INTERVAL_HOURS)) * 3600
    next_scan = time.monotonic() + 30
    next_settle = time.monotonic() + 1800

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

        if now >= next_settle:
            try:
                v57_settle()
                log.info("v5.7 settlement complete")
            except Exception:
                log.exception("v5.7 settlement failed")

            next_settle = time.monotonic() + 1800

        time.sleep(5)


if __name__ == "__main__":
    main()
