"""Pure functions over Echo360 JSON: current courses, lecture status, download feeds, file names."""
import re
from datetime import date, datetime, timedelta


def current_sections(enr: dict, today: date) -> list[dict]:
    # Echo360 marks both 26S1 and 26S2 isActive all year, so the half comes from the date.
    want = f"{today.year % 100:02d}S{1 if today.month <= 6 else 2}"
    terms = enr["termsById"]

    def current(t):
        name = t.get("name", "")
        return name == want or (t.get("isActive") and not re.search(r"S[12]$", name))

    return [s for s in enr["userSections"] if current(terms.get(s["termId"], {}))]


def order(sections: list[dict], opened: dict) -> list[dict]:
    return sorted(sections, key=lambda s: (-opened.get(s["sectionId"], 0), s["courseCode"]))


def status(lesson: dict, media: dict | None, now: datetime) -> str:
    if lesson.get("isFuture"):
        return "upcoming"
    if media:
        if media.get("isFailed"):
            return "failed"
        if media.get("isAvailable") and not media.get("isProcessing"):
            return "ready"
        return "processing"
    ended = datetime.fromisoformat(lesson["endTimeUTC"])
    return "missing" if now - ended > timedelta(hours=24) else "processing"


def lessons(syllabus: list, section_name: str, now: datetime) -> list[dict]:
    rows = []
    for x in syllabus:
        L = x.get("lesson")
        if x.get("type") != "SyllabusLessonType" or not L:
            continue
        meta = L["lesson"]
        start = meta["timing"]["start"]  # local time, e.g. 2026-09-28T15:00:00.000
        name = meta.get("displayName") or meta["name"]
        media = next((m for m in L.get("medias", []) if m.get("mediaType") == "Video"), None)
        rest = name.removeprefix(section_name + "-")
        label = re.sub(r"\W", "", rest.split("-")[0])[:12] if rest != name else ""
        rows.append({
            "id": meta["id"], "name": name, "date": start[:10],
            "label": label or start[11:16].replace(":", ""),
            "start_utc": L["startTimeUTC"], "status": status(L, media, now),
            "media": media["id"] if media else None,
        })
    return sorted(rows, key=lambda r: r["start_utc"], reverse=True)


def visible(rows: list[dict]) -> list[dict]:
    future = [r for r in rows if r["status"] == "upcoming"]
    nxt = future[-1]["id"] if future else None  # rows are newest-first, so the last one is the soonest
    return [r for r in rows if r["status"] != "upcoming" or r["id"] == nxt]


def feeds(info: dict) -> list[dict]:
    out = []
    for n, key in ((1, "primaryFiles"), (2, "secondaryFiles")):
        f = info.get(key)
        if f and f.get("files"):
            out.append({"n": n, "files": {x["label"]: x["fileName"] for x in f["files"]}})
    return out


def pick_file(feed: dict, q: str) -> str:
    return feed["files"].get("Full" if q == "full" else "Lower") or next(iter(feed["files"].values()))


def video_name(course: str, date: str, label: str, n: int, q: str) -> str:
    return f"{course}-{date}-{label}-s{n}-{q}.mp4"


def default_choice(n_feeds: int, same: bool, idle: set[int] = frozenset()) -> dict[str, str]:
    """First useful feed full, the next low. Drops a duplicate feed 2, and feeds stuck on a
    lectern idle screen (`idle`) unless that would leave nothing."""
    ns = [n for n in range(1, n_feeds + 1) if not (same and n == 2)]
    ns = [n for n in ns if n not in idle] or ns
    return {str(n): q for n, q in zip(ns, ("full", "low"))}


def short_label(label: str, width: int = 6) -> str:
    """Fit a label to the lecture column, keeping its number: 'Section9' -> 'Sect…9'."""
    if len(label) <= width:
        return label
    num = re.search(r"\d*$", label).group()
    return label[:width - 1 - len(num)] + "…" + num


def collapse(rows: list[dict], rank) -> list[dict]:
    """Echo360 sometimes records one lecture twice (two capture schedules, same slot and name).
    Keep one row per (date, label, name): the one `rank(row)` scores highest; ties keep the first."""
    best = {}
    for r in rows:
        k = (r["date"], r["label"], r["name"])
        if k not in best or rank(r) > rank(best[k]):
            best[k] = r
    keep = {id(r) for r in best.values()}
    return [r for r in rows if id(r) in keep]


def week_groups(rows: list[dict]) -> list[int]:
    """0/1 per row, flipping whenever the ISO week changes, so each week can share one colour."""
    out, prev, g = [], None, 1
    for r in rows:
        week = date.fromisoformat(r["date"]).isocalendar()[:2]
        if week != prev:
            prev, g = week, 1 - g
        out.append(g)
    return out


def latest_released(rows: list[dict]) -> datetime | None:
    ready = [r["start_utc"] for r in rows if r["status"] == "ready"]
    return datetime.fromisoformat(max(ready)) if ready else None


def when(t: datetime, now: datetime) -> str:
    """'Today 15:00', 'Yesterday 10:05', 'Wed 09:00' within a week, else '14-09-2026 09:00'."""
    days = (now.date() - t.date()).days
    day = {0: "Today", 1: "Yesterday"}.get(days) or (f"{t:%a}" if days < 7 else f"{t:%d-%m-%Y}")
    return f"{day} {t:%H:%M}"
