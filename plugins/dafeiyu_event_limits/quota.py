"""DeepSeek pricing schedule and a persistent, atomic reply ledger.

Only identifiers and timestamps are stored; no message bodies or credentials.
"""

import datetime as dt
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

BEIJING = dt.timezone(dt.timedelta(hours=8))
WINDOW = 1800
USER_LIMIT = 10
GROUP_LIMIT = 15
# Announced holiday periods, including their weekday compensatory days off.
# Weekend working days remain off-peak under DeepSeek's Saturday/Sunday rule.
# https://www.gov.cn/zhengce/content/202511/content_7047090.htm
HOLIDAY_PERIODS = {
    2026: (
        ("01-01", "01-03"),
        ("02-15", "02-23"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("06-19", "06-21"),
        ("09-25", "09-27"),
        ("10-01", "10-07"),
    ),
}


def is_peak(timestamp):
    local = dt.datetime.fromtimestamp(timestamp, BEIJING)
    if local.weekday() >= 5:
        return False
    day = local.strftime("%m-%d")
    if any(start <= day <= end for start, end in HOLIDAY_PERIODS.get(local.year, ())):
        return False
    # These fixed statutory dates also work before next year's calendar is added.
    if day in ("01-01", "05-01", "05-02", "10-01", "10-02", "10-03"):
        return False
    minute = local.hour * 60 + local.minute
    return 540 <= minute < 720 or 840 <= minute < 1080


class ReplyLimitExceeded(RuntimeError):
    """A reply was deliberately withheld; callers must not report delivery."""


class ReplyLedger:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS replies (
                turn TEXT PRIMARY KEY, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                at REAL NOT NULL, charged INTEGER NOT NULL, pending INTEGER NOT NULL
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS replies_user ON replies(user_id, at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS replies_group ON replies(group_id, at)"
            )
        Path(self.path).chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def clean(db, now):
        # A crashed or cancelled sender cannot keep a reservation indefinitely.
        db.execute(
            "DELETE FROM replies WHERE at <= ? OR (pending = 1 AND at <= ?)",
            (now - WINDOW, now - 600),
        )

    @staticmethod
    def reason(db, user_id, group_id, now):
        if not is_peak(now):
            return None
        for column, value, maximum, reason in (
            ("user_id", user_id, USER_LIMIT, "user"),
            ("group_id", group_id, GROUP_LIMIT, "group"),
        ):
            if value:
                count = db.execute(
                    "SELECT COUNT(*) FROM replies WHERE "
                    + column
                    + " = ? AND at > ? AND charged = 1",
                    (value, now - WINDOW),
                ).fetchone()[0]
                if count >= maximum:
                    return reason
        return None

    def blocked(self, user_id="", group_id="", turn=None):
        now = self.clock()
        with self.connect() as db:
            self.clean(db, now)
            if (
                turn
                and db.execute(
                    "SELECT 1 FROM replies WHERE turn = ?", (turn,)
                ).fetchone()
            ):
                return None
            return self.reason(db, str(user_id or ""), str(group_id or ""), now)

    def reserve(self, turn, user_id="", group_id=""):
        """Atomically reserve a slot before sending a reply.

        Args:
            turn: Stable identity shared by the parts of one reply.
            user_id: Replied-to QQ user, empty for unsolicited group messages.
            group_id: Destination group, empty for private messages.

        Returns:
            True for a new slot, False for another part of an existing reply.

        Raises:
            ReplyLimitExceeded: Either applicable peak quota is exhausted.
        """
        now = self.clock()
        user_id, group_id = str(user_id or ""), str(group_id or "")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.clean(db, now)
            if db.execute("SELECT 1 FROM replies WHERE turn = ?", (turn,)).fetchone():
                return False
            reason = self.reason(db, user_id, group_id, now)
            if reason:
                raise ReplyLimitExceeded("Peak reply limit reached: " + reason)
            db.execute(
                "INSERT INTO replies VALUES (?, ?, ?, ?, ?, 1)",
                (turn, user_id, group_id, now, int(is_peak(now))),
            )
        return True

    def retry_at(self, user_id="", group_id=""):
        """Return the next admission time without changing any reply quotas."""
        now = self.clock()
        if not is_peak(now):
            return now
        deadlines = []
        with self.connect() as db:
            self.clean(db, now)
            for column, value, maximum in (
                ("user_id", user_id, USER_LIMIT),
                ("group_id", group_id, GROUP_LIMIT),
            ):
                if not value:
                    continue
                rows = db.execute(
                    "SELECT at FROM replies WHERE "
                    + column
                    + " = ? AND at > ? AND charged = 1 ORDER BY at",
                    (str(value), now - WINDOW),
                ).fetchall()
                if len(rows) >= maximum:
                    deadlines.append(rows[len(rows) - maximum][0] + WINDOW)
        local = dt.datetime.fromtimestamp(now, BEIJING)
        peak_end = local.replace(
            hour=12 if local.hour < 12 else 18, minute=0, second=0, microsecond=0
        ).timestamp()
        return min(max(deadlines, default=now), peak_end)

    def finish(self, turn, success):
        with self.connect() as db:
            if success:
                # Keep the admission timestamp so boundary-crossing replies stay
                # in the pricing period in which they were admitted.
                db.execute("UPDATE replies SET pending = 0 WHERE turn = ?", (turn,))
            else:
                db.execute(
                    "DELETE FROM replies WHERE turn = ? AND pending = 1", (turn,)
                )

    def summary(self):
        now = self.clock()
        with self.connect() as db:
            self.clean(db, now)
            charged, pending = db.execute(
                "SELECT COALESCE(SUM(charged),0), COALESCE(SUM(pending),0) FROM replies"
            ).fetchone()
        local = dt.datetime.fromtimestamp(now, BEIJING)
        return {
            "period": "peak" if is_peak(now) else "off_peak",
            "beijing_time": local.isoformat(),
            "window_seconds": WINDOW,
            "user_limit": USER_LIMIT,
            "group_limit": GROUP_LIMIT,
            "charged_replies_in_window": charged,
            "pending_sends": pending,
            "holiday_calendar_years": sorted(HOLIDAY_PERIODS),
            "holiday_calendar_current": local.year in HOLIDAY_PERIODS,
        }
