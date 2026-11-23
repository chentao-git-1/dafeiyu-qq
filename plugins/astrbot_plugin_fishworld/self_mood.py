"""One overall mood and independently fading, source-bound feelings."""
import hashlib
import math
import re


BASELINE = 50
HALF_LIFE = 6 * 3600
REPLAY_WINDOW = 2 * 86400
MAX_MARKS = 256
MAX_FEELINGS = 8
MAX_TURN_FEELINGS = 3
HALF_LIVES = {"短": 2 * 3600, "中": HALF_LIFE, "长": 12 * 3600}
MARK = re.compile(r"[0-9a-f]{24}")
TONES = frozenset("平静 开心 愉快 轻快 期待 好奇 安心 放松 感动 惊喜 满足 惦记 担心 紧张 低落 失落 委屈 烦躁 生气 复杂".split())
WORLD_CAUSES = {"event", "world_do", "world_go", "world_say"}


def audience(origin):
    return hashlib.sha256(origin.encode()).hexdigest()[:24]


def marked(value):
    return isinstance(value, str) and MARK.fullmatch(value) is not None


def valid_cause(cause):
    if not isinstance(cause, dict):
        return False
    if not cause:
        return True
    if cause.get("kind") == "chat":
        return (set(cause) == {"kind", "mid", "start", "size", "digest"}
                and isinstance(cause["mid"], str) and 0 < len(cause["mid"]) <= 100
                and type(cause["start"]) is int and 0 <= cause["start"] <= 24000
                and type(cause["size"]) is int and 2 <= cause["size"] <= 64
                and marked(cause["digest"]))
    return (set(cause) == {"kind", "text"} and isinstance(cause["kind"], str)
            and cause["kind"] in WORLD_CAUSES and isinstance(cause["text"], str)
            and 2 <= len(cause["text"]) <= 64 and cause["text"].isprintable())


def valid_feeling(item):
    return (isinstance(item, dict) and set(item) == {"source", "scope", "tone", "strength", "span", "at", "cause"}
            and marked(item["source"]) and marked(item["scope"])
            and isinstance(item["tone"], str) and item["tone"] in TONES
            and type(item["strength"]) is int and 1 <= item["strength"] <= 10
            and isinstance(item["span"], str) and item["span"] in HALF_LIVES
            and number(item["at"]) and 0 <= item["at"] <= 32_503_680_000 and valid_cause(item["cause"]))


def number(value):
    return (type(value) is int and value.bit_length() < 1024) or (type(value) is float and math.isfinite(value))


def valid_mood(row):
    if row is None:
        return True
    base = {"score", "tone", "at", "floor", "seen"}
    if (not isinstance(row, dict) or set(row) not in (base, base | {"feelings"})
            or not all(number(row[k]) for k in ("score", "at", "floor"))):
        return False
    marks = row.get("seen")
    feelings = row.get("feelings", [])
    return (0 <= row["score"] <= 100 and isinstance(row.get("tone"), str) and row["tone"] in TONES
            and isinstance(feelings, list) and len(feelings) <= MAX_FEELINGS and all(valid_feeling(f) for f in feelings)
            and isinstance(marks, list) and len(marks) <= MAX_MARKS
            and all(isinstance(m, list) and len(m)==3 and number(m[0])
                    and all(isinstance(k,str) and MARK.fullmatch(k) for k in m[1:]) for m in marks))


def level(row, now):
    if row is None:
        return BASELINE
    elapsed = max(0, now - row["at"])
    return BASELINE + (row["score"] - BASELINE) * 0.5 ** (elapsed / HALF_LIFE)


def strength(item, now):
    return item["strength"] * 0.5 ** (max(0, now - item["at"]) / HALF_LIVES[item["span"]])


def ranked(items, now):
    return sorted((f for f in items if strength(f, now) >= 0.5),
                  key=lambda f: (strength(f, now), f["at"]), reverse=True)


def mood_view(row, now, viewer=None):
    score = int(level(row, now) + 0.5)
    if row and "feelings" in row:
        feelings = []
        tones = set()
        for item in ranked(row["feelings"], now):
            if item["tone"] in tones:
                continue
            tones.add(item["tone"])
            entry = {"tone": item["tone"], "strength": int(strength(item, now) + 0.5)}
            if viewer and viewer == item["scope"]:
                entry.update(source=item["source"], cause=dict(item["cause"]), at=item["at"])
            feelings.append(entry)
            if len(feelings) == MAX_TURN_FEELINGS:
                break
        if feelings:
            return {"score": score, "tone": feelings[0]["tone"], "feelings": feelings}
        return {"score": score, "tone": "平静"}
    tone = row["tone"] if row is not None else "平静"
    if row and score == BASELINE and now - row["at"] >= HALF_LIFE:
        tone = "平静"
    return {"score": score, "tone": tone}


def apply_feelings(row, turn, changes, now, scope):
    if (not marked(turn) or not marked(scope) or not isinstance(changes, list)
            or not 1 <= len(changes) <= MAX_TURN_FEELINGS):
        return None
    floor = row["floor"] if row else 0
    seen = [m for m in row["seen"] if m[0] >= now - REPLAY_WINDOW] if row else []
    if any(m[1] == turn for m in seen):
        return None
    previous_sources = {m[2] for m in seen}
    additions, stamps, resolved = {}, {}, set()
    total = 0
    for change in changes:
        if not isinstance(change, dict):
            continue
        source, stamp, delta = change.get("source"), change.get("at"), change.get("delta")
        if (not marked(source) or source in previous_sources or not number(stamp) or stamp <= floor
                or not now - REPLAY_WINDOW <= stamp <= now + 60 or type(delta) is not int or abs(delta) > 10):
            continue
        entry = {"source": source, "scope": scope, "at": min(stamp, now), "tone": change.get("tone"),
                 "strength": change.get("strength"), "span": change.get("span", "中"), "cause": change.get("cause", {})}
        if not valid_feeling(entry):
            continue
        identity = (source, entry["tone"])
        if identity in additions:
            continue
        additions[identity] = entry
        stamps[source] = stamp
        total += delta * 0.5 ** (max(0, now - stamp) / HALF_LIFE)
        target = change.get("settles")
        if (isinstance(target, tuple) and len(target) == 2 and marked(target[0])
                and isinstance(target[1], str) and target[1] in TONES):
            resolved.add(target)
    if not additions:
        return None
    # Scalar-era records contribute their score without inventing a past intensity or cause.
    old = row.get("feelings", []) if row else []
    kept = [f for f in old if (f["source"], f["tone"]) not in additions
            and not (f["scope"] == scope and (f["source"], f["tone"]) in resolved)]
    active = ranked([*additions.values(), *kept], now)[:MAX_FEELINGS]
    seen.extend([stamp, turn, source] for source, stamp in stamps.items())
    if len(seen) > MAX_MARKS:
        seen.sort(key=lambda mark: mark[0])
        floor = max(floor, seen[-MAX_MARKS-1][0])
        seen = seen[-MAX_MARKS:]
    score = min(100, max(0, level(row, now) + max(-10, min(10, total))))
    return {"score": round(score, 4), "tone": active[0]["tone"] if active else "平静",
            "at": max(now, row["at"]) if row else now, "floor": floor, "seen": seen, "feelings": active}


def next_mood(row, turn, source, occurred, delta, tone, now):
    """Accept an in-flight reply using the original single-feeling protocol."""
    if type(delta) is not int or (delta == 0 and tone == mood_view(row, now)["tone"]):
        return None
    return apply_feelings(row, turn, [{"source": source, "at": occurred, "tone": tone,
                                      "delta": delta, "strength": max(1, abs(delta))}], now, "0"*24)
