"""Tinglan's short-lived mood, carried in her existing bound FishWorld state."""
import math
import re


BASELINE = 50
HALF_LIFE = 6 * 3600
REPLAY_WINDOW = 2 * 86400
MAX_MARKS = 256
MARK = re.compile(r"[0-9a-f]{24}")
TONES = frozenset("平静 开心 愉快 轻快 期待 好奇 安心 放松 感动 惊喜 满足 惦记 担心 紧张 低落 失落 委屈 烦躁 生气 复杂".split())


def number(value):
    return (type(value) is int and value.bit_length() < 1024) or (type(value) is float and math.isfinite(value))


def valid_mood(row):
    if row is None:
        return True
    if (not isinstance(row, dict) or set(row) != {"score", "tone", "at", "floor", "seen"}
            or not all(number(row[k]) for k in ("score", "at", "floor"))):
        return False
    marks = row.get("seen")
    return (0 <= row["score"] <= 100 and isinstance(row.get("tone"), str) and row["tone"] in TONES
            and isinstance(marks, list) and len(marks) <= MAX_MARKS
            and all(isinstance(m, list) and len(m)==3 and number(m[0])
                    and all(isinstance(k,str) and MARK.fullmatch(k) for k in m[1:]) for m in marks))


def level(row, now):
    if row is None:
        return BASELINE
    elapsed = max(0, now - row["at"])
    return BASELINE + (row["score"] - BASELINE) * 0.5 ** (elapsed / HALF_LIFE)


def mood_view(row, now):
    score = int(level(row, now) + 0.5)
    tone = row["tone"] if row is not None else "平静"
    if row and score == BASELINE and now - row["at"] >= HALF_LIFE:
        tone = "平静"
    return {"score": score, "tone": tone}


def next_mood(row, turn, source, occurred, delta, tone, now):
    if (not all(isinstance(k,str) and MARK.fullmatch(k) for k in (turn,source))
            or not number(occurred) or not now - REPLAY_WINDOW <= occurred <= now + 60
            or type(delta) is not int or not -10 <= delta <= 10
            or not isinstance(tone,str) or tone not in TONES):
        return None
    floor = row["floor"] if row else 0
    seen = [m for m in row["seen"] if m[0] >= now - REPLAY_WINDOW] if row else []
    if any(m[1] == turn or m[2] == source for m in seen):
        return None
    if len(seen) >= MAX_MARKS:
        # A delayed replay must stay ineligible even after its hash is evicted.
        floor = max(floor, min(m[0] for m in seen))
        seen = [m for m in seen if m[0] > floor]
    if occurred <= floor or (delta == 0 and tone == mood_view(row,now)["tone"]):
        return None
    updated = min(100, max(0, level(row, now) + delta))
    return {"score": round(updated, 4), "tone": tone, "at": max(now,row["at"]) if row else now,
            "floor": floor, "seen": [*seen, [occurred,turn,source]]}
