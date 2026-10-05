from datetime import date, datetime, timedelta, timezone

from echotui import model

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

ENR = {
    "termsById": {
        "t1": {"name": "26S1", "isActive": True},
        "t2": {"name": "26S2", "isActive": True},
        "t0": {"name": "25S2", "isActive": False},
        "fy": {"name": "26FY", "isActive": True},
    },
    "userSections": [
        {"sectionId": "a", "sectionName": "COSC264-26S2", "courseCode": "COSC264", "termId": "t2"},
        {"sectionId": "b", "sectionName": "COSC262-26S1", "courseCode": "COSC262", "termId": "t1"},
        {"sectionId": "c", "sectionName": "MATH120-25S2", "courseCode": "MATH120", "termId": "t0"},
        {"sectionId": "d", "sectionName": "ENGR199-26FY", "courseCode": "ENGR199", "termId": "fy"},
    ],
}


def ids(xs):
    return [x["sectionId"] if "sectionId" in x else x["id"] for x in xs]


def test_current_sections_second_half():
    assert ids(model.current_sections(ENR, date(2026, 9, 28))) == ["a", "d"]


def test_current_sections_first_half():
    assert ids(model.current_sections(ENR, date(2026, 3, 1))) == ["b", "d"]


def test_order_recent_first_then_alphabetical():
    secs = model.current_sections(ENR, date(2026, 9, 28))
    assert ids(model.order(secs, {"d": 5.0})) == ["d", "a"]
    assert ids(model.order(secs, {})) == ["a", "d"]


def lesson(id, start, end_utc, name="COSC264-26S2-LecA-Intro", future=False, medias=()):
    return {"type": "SyllabusLessonType", "lesson": {
        "lesson": {"id": id, "name": name, "timing": {"start": start, "end": start}},
        "medias": list(medias), "isFuture": future, "isPast": not future,
        "startTimeUTC": end_utc, "endTimeUTC": end_utc}}


def media(**kw):
    return {"id": "m1", "mediaType": "Video", "isAvailable": False, "isProcessing": False, "isFailed": False, **kw}


def one(x):
    return model.lessons([x], "COSC264-26S2", NOW)[0]


def test_status_rules():
    t = "2026-09-28T15:00:00.000"
    assert one(lesson("1", t, "2026-09-29T02:00:00.000Z", future=True))["status"] == "upcoming"
    assert one(lesson("1", t, "2026-09-28T02:00:00.000Z", medias=[media(isAvailable=True)]))["status"] == "ready"
    assert one(lesson("1", t, "2026-09-28T02:00:00.000Z", medias=[media(isProcessing=True)]))["status"] == "processing"
    assert one(lesson("1", t, "2026-09-28T02:00:00.000Z", medias=[media(isFailed=True)]))["status"] == "failed"
    assert one(lesson("1", t, "2026-09-28T11:00:00.000Z"))["status"] == "processing"
    assert one(lesson("1", t, "2026-09-26T11:00:00.000Z"))["status"] == "missing"


def test_row_fields_and_label():
    r = one(lesson("L1", "2026-09-28T15:00:00.000", "2026-09-28T02:00:00.000Z",
                   name="COSC264-26S2-LecB-Intro to nets", medias=[media(isAvailable=True)]))
    assert (r["id"], r["date"], r["label"], r["media"]) == ("L1", "2026-09-28", "LecB", "m1")
    r = one(lesson("L2", "2026-09-28T15:00:00.000", "2026-09-28T02:00:00.000Z", name="Guest talk"))
    assert r["label"] == "1500"


def test_lessons_newest_first_and_visible_keeps_next_future_only():
    rows = model.lessons([
        lesson("past", "2026-09-27T10:00:00.000", "2026-09-26T21:00:00.000Z"),
        lesson("next", "2026-09-29T10:00:00.000", "2026-09-28T21:00:00.000Z", future=True),
        lesson("later", "2026-10-05T10:00:00.000", "2026-10-04T21:00:00.000Z", future=True),
    ], "COSC264-26S2", NOW)
    assert ids(rows) == ["later", "next", "past"]
    assert ids(model.visible(rows)) == ["next", "past"]


INFO = {
    "primaryFiles": {"posterUrl": "p1", "files": [{"fileName": "sd1.mp4", "label": "Lower"},
                                                   {"fileName": "hd1.mp4", "label": "Full"}]},
    "secondaryFiles": {"posterUrl": "p2", "files": [{"fileName": "sd2.mp4", "label": "Lower"},
                                                     {"fileName": "hd2.mp4", "label": "Full"}]},
}


def test_feeds_and_pick_file():
    fs = model.feeds(INFO)
    assert [f["n"] for f in fs] == [1, 2]
    assert model.pick_file(fs[1], "full") == "hd2.mp4" and model.pick_file(fs[1], "low") == "sd2.mp4"
    assert len(model.feeds({"primaryFiles": INFO["primaryFiles"]})) == 1


def test_pick_file_falls_back_to_any_file_when_label_missing():
    feed = {"files": {"Full": "hd1.mp4"}}
    assert model.pick_file(feed, "low") == "hd1.mp4"


def test_video_name():
    assert model.video_name("COSC264", "2026-09-28", "LecA", 1, "full") == "COSC264-2026-09-28-LecA-s1-full.mp4"
    assert model.video_name("COSC264", "2026-09-28", "LecA", 2, "low") == "COSC264-2026-09-28-LecA-s2-low.mp4"


def test_default_choice():
    assert model.default_choice(1, False) == {"1": "full"}
    assert model.default_choice(2, True) == {"1": "full"}
    assert model.default_choice(2, False) == {"1": "full", "2": "low"}


def test_when():
    now = datetime(2026, 9, 28, 20, 0)
    assert model.when(datetime(2026, 9, 28, 15, 0), now) == "Today 15:00"
    assert model.when(datetime(2026, 9, 27, 10, 5), now) == "Yesterday 10:05"
    assert model.when(datetime(2026, 9, 29, 9, 0), now) == "Tomorrow 09:00"
    assert model.when(datetime(2026, 9, 23, 9, 0), now) == "Wed 09:00"
    assert model.when(datetime(2026, 9, 14, 9, 0), now) == "14-09-2026 09:00"


def test_timeline_is_newest_first_and_stops_at_the_end_of_today():
    rows = [{"id": "tomorrow", "date": "2026-09-29", "start_utc": "2026-09-29T02:00:00.000Z"},
            {"id": "tonight", "date": "2026-09-28", "start_utc": "2026-09-28T08:00:00.000Z"},
            {"id": "today", "date": "2026-09-28", "start_utc": "2026-09-28T02:00:00.000Z"},
            {"id": "3 days", "date": "2026-09-25", "start_utc": "2026-09-25T02:00:00.000Z"},
            {"id": "4 days", "date": "2026-09-24", "start_utc": "2026-09-24T02:00:00.000Z"}]
    today = date(2026, 9, 28)
    assert [r["id"] for r in model.timeline(rows, today, 3)] == ["tonight", "today", "3 days"]
    assert [r["id"] for r in model.timeline(rows, today, 6)][-1] == "4 days"


def test_later_today_uses_the_local_date():
    nz = timezone(timedelta(hours=13))
    now = datetime(2026, 9, 28, 10, tzinfo=nz)
    at = lambda h: {"start_utc": (datetime(2026, 9, 28, h, tzinfo=nz)).astimezone(timezone.utc).isoformat()}  # noqa: E731
    assert model.later_today([at(15)], now)
    assert not model.later_today([at(9)], now)  # already started
    assert not model.later_today([{"start_utc": "2026-09-28T11:30:00+00:00"}], now)  # 00:30 tomorrow local


def test_default_choice_skips_login_screen_feeds():
    assert model.default_choice(2, False, {1}) == {"2": "full"}
    assert model.default_choice(2, False, {2}) == {"1": "full"}
    assert model.default_choice(2, False, {1, 2}) == {"1": "full", "2": "low"}  # nothing better: keep default


def test_week_groups_flip_per_iso_week():
    dates = ["2026-09-30", "2026-09-28", "2026-09-25", "2026-09-22", "2026-09-21", "2026-09-18"]
    assert model.week_groups([{"date": d} for d in dates]) == [0, 0, 1, 1, 1, 0]


def test_short_label_fits_column_and_keeps_number():
    assert model.short_label("LecA") == "LecA"
    assert model.short_label("Section9") == "Sect…9"
    assert model.short_label("Section12") == "Sec…12"
    assert model.short_label("Workshop") == "Works…"


def test_collapse_keeps_best_ranked_duplicate():
    rows = [{"id": "a", "date": "2026-09-24", "label": "LecB", "name": "X"},
            {"id": "b", "date": "2026-09-24", "label": "LecB", "name": "X"},
            {"id": "c", "date": "2026-09-23", "label": "LecA", "name": "X"}]
    assert [r["id"] for r in model.collapse(rows, lambda r: r["id"] == "b")] == ["b", "c"]
    assert [r["id"] for r in model.collapse(rows, lambda r: 0)] == ["a", "c"]  # tie keeps the first


def test_day():
    today = date(2026, 9, 28)
    assert [model.day(d, today) for d in ("2026-09-29", "2026-09-28", "2026-09-27", "2026-09-23")] == [
        "Tomorrow", "Today", "Yesterday", "Wed 23-09"]
