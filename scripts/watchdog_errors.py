"""Count AstrBot error events in timestamped Docker stdout and stderr."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import re
import subprocess
import time


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LEVEL = r"(?:DBUG|DEBU|DEBUG|INFO|WARN(?:ING)?|ERRO(?:R)?|CRIT(?:ICAL)?|FATAL)"
HEADER = re.compile(r"^(?:\[[^\]\n]+\]\s+)*?\[(" + LEVEL + r")\]\s*:?[ \t]*(.*)$")
ERROR_LEVELS = {"ERRO", "ERROR", "CRIT", "CRITICAL", "FATAL"}
EXCEPTION = re.compile(r"^\s*(?:\|\s*)*([\w.]*(?:Error|Exception|Disconnect|Exit|Interrupt)(?::.*)?)\s*$")
TRACE = "Traceback (most recent call last):"


@dataclass
class ErrorEvent:
    when: float | None
    layer: str
    category: str
    cause: str = ""


def _instant(stamp):
    if "." in stamp and stamp.endswith("Z"):
        stamp = stamp[:26].rstrip("Z") + "Z"
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _stream_events(stream):
    current = None
    chained = False
    non_error_header = None
    for line in stream.splitlines():
        stamp, _, message = line.partition(" ")
        if not stamp.endswith("Z"):
            stamp, message = "", line
        message = ANSI.sub("", message)
        header = HEADER.match(message)
        if header:
            if current:
                yield current
            current, chained, non_error_header = None, False, None
            level, body = header.groups()
            if level not in ERROR_LEVELS:
                non_error_header = stamp
                continue
            if "Error in ASGI Framework" in body:
                layer = "asgi_handler" if "[hypercorn.logging:" in body else "asgi_stderr"
                current = ErrorEvent(_instant(stamp), layer, "服务框架")
            elif "Unhandled exception in dashboard API" in body:
                current = ErrorEvent(_instant(stamp), "dashboard", "管理面板")
            else:
                category = "模型连接" if "Network connection failed" in body else "其他服务"
                current = ErrorEvent(_instant(stamp), "application", category)
        elif message.strip() == TRACE:
            if current is None and non_error_header is not None:
                # A logger can attach the same exception to an INFO or WARN
                # record. Docker timestamps its continuation lines separately.
                now, logged = _instant(stamp), _instant(non_error_header)
                if now is None or logged is None or abs(now - logged) <= 2:
                    continue
                non_error_header = None
            if current is None or (current.layer == "bare" and current.cause and not chained):
                if current:
                    yield current
                current = ErrorEvent(_instant(stamp), "bare", "其他服务")
            chained = False
        elif current:
            if message.startswith(("During handling of the above exception", "The above exception was the direct cause")):
                chained = True
            else:
                cause = EXCEPTION.fullmatch(message)
                if cause:
                    current.cause = cause[1]
    if current:
        yield current


def error_counts(stdout, stderr):
    """Merge a dashboard exception with its Hypercorn mirrors, at most once per layer."""
    events = [*_stream_events(stdout), *_stream_events(stderr)]
    wrappers = {"asgi_handler", "asgi_stderr"}
    counts = Counter(event.category for event in events if event.layer not in wrappers)
    groups = [(event, set()) for event in events if event.layer == "dashboard"]
    for event in (event for event in events if event.layer in wrappers):
        matches = [(abs(event.when - prior.when), layers) for prior, layers in groups
                   if event.when is not None and prior.when is not None and event.cause
                   and event.cause == prior.cause and abs(event.when - prior.when) <= 2
                   and event.layer not in layers]
        if matches:
            min(matches, key=lambda match: match[0])[1].add(event.layer)
        else:
            counts[event.category] += 1
            groups.append((event, {event.layer}))
    return counts


def alert_text(counts):
    details = "，".join(f"{name} {n} 次" for name, n in sorted(counts.items()) if n)
    return f"【大肥鱼监控】最近 30 分钟记录到 {sum(counts.values())} 次错误（{details}）。"


def check_recent_errors(state, notify, log, *, interval, threshold):
    now = time.time()
    status = state.setdefault("errors", {"last_check": 0, "last_alert": 0})
    if now - float(status.get("last_check") or 0) < interval:
        return
    status["last_check"] = now
    try:
        output = subprocess.run(
            ["docker", "logs", "--timestamps", "--since", "30m", "dafeiyu-astrbot"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log(f"读取日志失败：{type(error).__name__}")
        return
    counts = error_counts(output.stdout, output.stderr)
    count = sum(counts.values())
    if count >= threshold and now - float(status.get("last_alert") or 0) >= 3600:
        notify(alert_text(counts))
        status["last_alert"] = now
        log(f"错误率提醒：{count} 次")
