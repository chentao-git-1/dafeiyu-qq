"""Persistent DeepSeek balance alerts with exact amounts and bounded notices."""

import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import time
import uuid
from decimal import Decimal

OWNER = "7000000001"
BOT = "7000000002"
THRESHOLD = Decimal("1.00")
HELP_THRESHOLD = Decimal("0.70")
INTERVAL = 600
REPEAT_AFTER = 2 * 3600
PAUSE_FOR = 2 * 3600
TZ = dt.timezone(dt.timedelta(hours=8))
TERMINAL = {"sent", "unknown", "sending", "skipped"}
NUMBER = re.compile(r"-?\d{1,12}(?:\.\d{1,12})?")


def balance_snapshot(data):
    """Read only a validated CNY total; missing currencies are not zero."""
    if not isinstance(data, dict) or not isinstance(data.get("is_available"), bool):
        raise ValueError("Invalid balance response")
    rows = data.get("balance_infos")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("Invalid balance rows")
    currencies = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid balance row")
        currency = row.get("currency")
        if currency not in ("CNY", "USD") or currency in currencies:
            raise ValueError("Invalid balance currency")
        for field in ("total_balance", "topped_up_balance", "granted_balance"):
            value = row.get(field)
            if not isinstance(value, str) or not NUMBER.fullmatch(value):
                raise ValueError("Invalid balance amount")
        currencies[currency] = Decimal(row["total_balance"])
    if "CNY" not in currencies:
        raise ValueError("CNY balance unavailable")
    return currencies["CNY"], data["is_available"]


def eligible_contacts(contacts, allowed):
    """Keep ranked, privately reachable contacts; names do not confer eligibility."""
    result = {}
    for row in contacts:
        qq = str(row.get("qq", ""))
        if (
            qq not in (OWNER, BOT)
            and re.fullmatch(r"[1-9]\d{4,11}", qq)
            and "dafeiyu-qq:FriendMessage:" + qq in allowed
        ):
            result.setdefault(qq, {"qq": qq, "name": "朋友"})
    return list(result.values())


class BalanceWatch:
    """Plan notifications and record reservations before any outbound request."""

    def __init__(self, path, clock=time.time, chooser=None):
        self.path = Path(path)
        self.clock = clock
        self.contacts = {}
        self.state = {"version": 2, "cycle": None, "daily_help": {}, "help_days": {}}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("version") not in (1, 2) or "cycle" not in self.state:
                raise ValueError("Unsupported balance reminder state")
            if self.state['version'] == 1:
                # Retain an exact local backup before changing the state schema.
                backup = self.path.with_suffix('.v1.bak')
                if not backup.exists():
                    with backup.open('x', encoding='utf-8') as stream:
                        stream.write(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
                    backup.chmod(0o600)
                self.state['version'] = 2
                daily = self.state.get('daily_help', {})
                self.state['help_days'] = {daily['day']: daily['cycle']} if daily.get('day') and daily.get('cycle') else {}
                cycle = self.state.get('cycle')
                if cycle:
                    attempted = any(key.startswith('help:') for key in cycle.get('notices', {}))
                    cycle['helpers_locked'] = attempted
                    if not attempted:
                        cycle['targets'] = []
                    if cycle['status'] == 'recovered' and Decimal(self.state.get('last_balance', '1')) < THRESHOLD:
                        cycle['status'] = 'low'
                        cycle.pop('recovered_at', None)
            cycle = self.state.get("cycle")
            if cycle:
                for notice in cycle.get("notices", {}).values():
                    if notice.get("status") == "sending":
                        notice["status"] = "unknown"
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        temporary = self.path.with_name("state-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def observe(self, amount, available, contacts):
        now = self.clock()
        # None means contact discovery failed; never freeze an unknown list as empty.
        self.contacts = {row["qq"]: row for row in (contacts or [])}
        self.state.update(
            last_checked_at=now,
            last_balance=format(amount, "f"),
            last_available=available,
            last_error=None,
        )
        cycle = self.state.get("cycle")
        if amount < THRESHOLD:
            if not cycle or cycle["status"] != "low":
                if cycle:
                    self.state["last_cycle"] = cycle
                self.state["cycle"] = {
                    "id": uuid.uuid4().hex,
                    "status": "low",
                    "low_since": now,
                    "pause_until": 0,
                    "targets": [],
                    "helpers_locked": False,
                    "notices": {},
                }
            cycle = self.state['cycle']
            if amount < HELP_THRESHOLD and not cycle.get('helpers_locked') and contacts is not None:
                cycle['targets'] = copy.deepcopy(list(self.contacts.values())[:2])
                cycle['helpers_locked'] = True
                cycle['help_entered_at'] = now
            if amount < HELP_THRESHOLD and cycle.get('helpers_locked') and contacts is not None:
                for person in cycle['targets']:
                    if person['qq'] not in self.contacts:
                        self.skip_helper(person['qq'])
        elif cycle and cycle["status"] == "low":
            cycle["status"] = "recovered"
            cycle["recovered_at"] = now
            cycle["pause_until"] = 0
            if cycle["notices"].get("first", {}).get("status") not in TERMINAL:
                self.state["last_cycle"] = cycle
                self.state["cycle"] = None
        self.save()
        return self.plan()

    def record_error(self, error_type):
        self.state.update(last_error=str(error_type), last_error_at=self.clock())
        self.save()

    def daytime(self):
        hour = dt.datetime.fromtimestamp(self.clock(), TZ).hour
        return 9 <= hour < 21

    def due(self, key):
        notice = self.state["cycle"]["notices"].get(key)
        return not notice or (
            notice.get("status") == "failed"
            and self.clock() - notice.get("attempted_at", 0) >= INTERVAL
        )

    def action(self, kind, qq=OWNER, name="爸爸", critical=False):
        cycle = self.state["cycle"]
        return {
            "cycle": cycle["id"],
            "key": kind if qq == OWNER else "help:" + qq,
            "kind": kind,
            "qq": qq,
            "name": name,
            "critical": critical,
        }

    def plan(self):
        cycle = self.state.get("cycle")
        if (not cycle or self.state.get('last_error')
                or self.clock() - self.state.get('last_checked_at', 0) > INTERVAL):
            return []
        notices = cycle["notices"]
        first = notices.get("first", {})
        if cycle["status"] == "recovered":
            if first.get("status") in TERMINAL and self.due("recovered"):
                return [self.action("recovered")]
            return []
        if self.clock() < cycle.get("pause_until", 0):
            return []
        amount = Decimal(self.state["last_balance"])
        critical = amount <= 0 or not self.state["last_available"]
        actions = []
        if self.due("first"):
            actions.append(self.action("first", critical=critical))
        elif first.get('status') in TERMINAL and critical and self.due("exhausted"):
            actions.append(self.action("exhausted", critical=True))
        elapsed = self.clock() - first.get("sent_at", first.get("attempted_at", self.clock()))
        if first.get('status') in TERMINAL and self.daytime() and elapsed >= REPEAT_AFTER and self.due("repeat"):
            actions.append(self.action("repeat"))
        day = dt.datetime.fromtimestamp(self.clock(), TZ).date().isoformat()
        round_allowed = self.state.get('help_days', {}).get(day) in (None, cycle['id'])
        if self.daytime() and amount < HELP_THRESHOLD and round_allowed:
            for person in cycle["targets"]:
                qq = person["qq"]
                if qq in self.contacts and self.due("help:" + qq):
                    actions.append(self.action("help", qq, person["name"]))
        return actions

    def reserve(self, action):
        cycle = self.state.get("cycle")
        if not cycle or action["cycle"] != cycle["id"] or not self.due(action["key"]):
            return False
        if action not in self.plan():
            return False
        previous = cycle["notices"].get(action["key"], {})
        cycle["notices"][action["key"]] = {
            "status": "sending",
            "attempted_at": self.clock(),
            "attempts": previous.get("attempts", 0) + 1,
            "critical": action["critical"],
        }
        if action["kind"] == "first" and action["critical"]:
            cycle["notices"]["exhausted"] = copy.deepcopy(
                cycle["notices"][action["key"]]
            )
        if action["kind"] == "help":
            self.state["daily_help"] = {
                "day": dt.datetime.fromtimestamp(self.clock(), TZ).date().isoformat(),
                "cycle": cycle["id"],
            }
            days = self.state.setdefault('help_days', {})
            days[self.state['daily_help']['day']] = cycle['id']
            cutoff = (dt.datetime.fromtimestamp(self.clock(), TZ).date() - dt.timedelta(days=31)).isoformat()
            self.state['help_days'] = {day: value for day, value in days.items() if day >= cutoff}
        self.save()
        return True

    def skip_helper(self, qq):
        cycle = self.state.get('cycle')
        key = 'help:' + str(qq)
        if cycle and self.due(key):
            cycle['notices'][key] = {'status': 'skipped', 'attempted_at': self.clock()}
            self.save()

    def complete(self, action, status, message_id=None, text=None):
        cycle = self.state.get("cycle")
        if not cycle or cycle["id"] != action["cycle"]:
            return
        notice = cycle["notices"][action["key"]]
        notice["status"] = status
        if status == "sent":
            notice["sent_at"] = self.clock()
            notice["message_id"] = message_id
            if isinstance(text,str) and 1<=len(text.strip())<=1500:
                notice['text']=text.strip()
        if action["kind"] == "first" and action["critical"]:
            cycle["notices"]["exhausted"] = copy.deepcopy(notice)
        if action["kind"] == "exhausted" and status in TERMINAL:
            first = cycle["notices"].get("first", {})
            elapsed = self.clock() - first.get("sent_at", first.get("attempted_at", self.clock()))
            if elapsed >= REPEAT_AFTER:
                cycle["notices"]["repeat"] = {"status": "skipped"}
        if action["kind"] == "recovered" and status in TERMINAL:
            self.state["last_cycle"] = cycle
            self.state["cycle"] = None
        self.save()

    def pause(self):
        cycle = self.state.get("cycle")
        if not cycle or cycle["status"] != "low":
            return False
        cycle["pause_until"] = self.clock() + PAUSE_FOR
        self.save()
        return True

    def resume(self):
        cycle = self.state.get("cycle")
        if cycle:
            cycle["pause_until"] = 0
            self.save()

    def status(self):
        cycle = self.state.get("cycle")
        return {
            "threshold_cny": str(THRESHOLD),
            "helper_threshold_cny": str(HELP_THRESHOLD),
            "comparison": "strictly_less_than",
            "check_interval_seconds": INTERVAL,
            "last_checked_at": self.state.get("last_checked_at"),
            "last_balance_cny": self.state.get("last_balance"),
            "last_available": self.state.get("last_available"),
            "last_error": self.state.get("last_error"),
            "cycle": copy.deepcopy(cycle),
            "eligible_ranked_contacts": list(self.contacts.values()),
        }


def notice_text(action, balance):
    if action['kind'] == 'help':
        return '鲸鲸的饭钱快不够啦，帮我提醒爸爸添一点嘛～'
    if action["critical"]:
        return (
            "爸爸，鲸鲸的 DeepSeek 饭钱见底啦，这会儿可能接不上话。"
            "有空给我续一点饭钱嘛，充好后我会再查一下～"
            "\n当前可用余额：{} 元。".format(balance)
        )
    if action["kind"] == "first":
        return (
            "爸爸，鲸鲸的饭盒快见底啦，记得添点 DeepSeek 饭钱～"
            "\n当前可用余额：{} 元，已经低于 1.00 元。".format(balance)
        )
    if action["kind"] == "repeat":
        return "爸爸是不是忙忘啦，我这边的饭钱还不足一元呢。有空给鲸鲸添一点嘛～"
    return "饭钱够啦！谢谢爸爸，鲸鲸先安心吃饭～"
