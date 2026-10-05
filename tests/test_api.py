"""The web API, over a real HTTP server."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime

import httpx
import pytest

from prack.api import create_app

from .webrig import make_runtime, populate, serve


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    runtime = make_runtime(tmp_path_factory.mktemp("api"))
    extras = populate(runtime)
    app = create_app(runtime, stream_interval=0.05, full_every=0.5, today=lambda: date(2026, 7, 20))
    with serve(app) as (url, server):
        yield {"url": url, "runtime": runtime, "server": server, **extras}


@pytest.fixture
def client(site):
    with httpx.Client(base_url=site["url"], timeout=20) as c:
        yield c


def stream_messages(client: httpx.Client, path: str, count: int) -> list[dict]:
    messages = []
    with client.stream("GET", path) as response:
        assert response.status_code == 200
        data = None
        for line in response.iter_lines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
            elif line == "" and data is not None:
                messages.append(data)
                data = None
                if len(messages) >= count:
                    break
    return messages


# ---------------------------------------------------------------- small endpoints


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and abs(r.json()["time"] - time.time()) < 5


def test_health_says_503_when_the_database_is_gone(site, client, monkeypatch):
    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(site["runtime"].db, "session", broken)
    r = client.get("/api/health")
    assert r.status_code == 503 and "database unavailable" in r.json()["detail"]


def test_config_has_what_the_map_needs(client):
    j = client.get("/api/config").json()
    assert j["version"] and j["live_window_s"] == 600 and j["trail_points"] == 360 and j["auth"] is False
    (region,) = j["regions"]
    assert (region["id"], region["timezone"], region["bbox"]) == ("ch", "Europe/Zurich", [5.85, 45.75, 10.55, 47.85])
    assert region["center"] == [8.23, 46.8] and region["default_basemap"] == "swisstopo-grey"
    assert [m["id"] for m in region["basemaps"]] == ["swisstopo-grey", "swisstopo-colour", "swisstopo-aerial"]
    assert "wmts.geo.admin.ch" in region["basemaps"][0]["tiles"][0]
    assert j["terrain"] == {
        "url": "/api/dem/{z}/{x}/{y}.png",
        "encoding": "terrarium",
        "tile_size": 256,
        "max_zoom": 12,
    }


def test_status_is_the_runtime_status(client):
    j = client.get("/api/status").json()
    assert set(j) >= {"version", "link", "counters", "drops", "sources", "tracker", "ddb", "queue", "database"}
    assert j["ddb"]["devices"] == 2 and j["link"]["state"] == "off"
    assert j["sources"]["groups"] and j["counters"]["flights.opened"] >= 5


def test_the_openapi_description_is_there_but_not_a_cdn_loading_docs_page(client):
    assert client.get("/api/openapi.json").json()["info"]["title"] == "prack"
    assert client.get("/docs").status_code == 404


def test_responses_carry_security_headers(client):
    h = client.get("/api/health").headers
    assert (
        h["x-content-type-options"] == "nosniff"
        and h["x-frame-options"] == "DENY"
        and h["referrer-policy"] == "same-origin"
    )


# ---------------------------------------------------------------- the calendar


def test_days_count_listed_flights_per_local_day(client):
    r = client.get("/api/days", params={"start": "2026-07-01", "end": "2026-07-31"})
    assert r.json() == [
        {"date": "2026-07-14", "total": 1},
        {"date": "2026-07-15", "total": 3},
        {"date": "2026-07-16", "total": 1},  # the open flight, once it has climbed
    ]


def test_a_drive_and_a_hop_are_not_listed(site, client):
    listed = {f["id"] for f in client.get("/api/days/2026-07-15/flights").json()}
    assert len(listed) == 3 and site["drive"] not in listed and site["hop"] not in listed  # both are dated 07-15


def test_days_default_to_the_last_ninety_days_up_to_today(client):
    j = client.get("/api/days").json()  # "today" is 2026-07-20 in these tests
    assert [d["date"] for d in j] == ["2026-07-14", "2026-07-15", "2026-07-16"]
    assert client.get("/api/days", params={"end": "2026-07-14"}).json() == [{"date": "2026-07-14", "total": 1}]
    assert client.get("/api/days", params={"start": "2026-07-16"}).json() == [{"date": "2026-07-16", "total": 1}]


def test_days_can_be_limited_to_a_region(client):
    params = {"start": "2026-07-01", "end": "2026-07-31"}
    assert sum(d["total"] for d in client.get("/api/days", params={**params, "region": "ch"}).json()) == 5
    assert client.get("/api/days", params={**params, "region": "at"}).json() == []


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-08-01", "end": "2026-07-01"},
        {"start": "2024-01-01", "end": "2026-07-01"},
        {"start": "yesterday"},
        {"end": "2026-13-01"},
    ],
)
def test_days_reject_nonsense(client, params):
    assert client.get("/api/days", params=params).status_code == 422


# ---------------------------------------------------------------- the flights of a day


def test_a_day_lists_its_flights_in_order_with_labels_stats_and_previews(client):
    flights = client.get("/api/days/2026-07-15/flights").json()
    assert [f["callsign"] for f in flights] == ["FLRD00001", "FLRD00002", "FNTD00003"]  # by start time
    mia, hb, anon = flights
    assert (mia["label"], mia["pilot"], mia["source"]) == ("Mia", "Mia", "FLARM")
    assert (hb["label"], hb["reg"], hb["cn"], hb["model"]) == ("X1 HB-3123", "HB-3123", "X1", "Ozone Rush")
    assert (anon["label"], anon["reg"], anon["cn"]) == ("FNTD00003", None, None)  # opted out of being identified
    assert anon["model"] == "Gin Boomerang"
    for f in flights:
        assert (f["date"], f["region"], f["status"], f["close_reason"], f["airborne"], f["live"]) == (
            "2026-07-15", "ch", "closed", "landed", True, False,
        )  # fmt: skip
        assert f["utc_offset_s"] == 7200 and f["start"] < f["takeoff"]["t"] < f["landing"]["t"] <= f["end"]
        s = f["stats"]
        assert (
            s["fix_count"] > 200  # a FANET-only pilot sends one position every 4 s
            and s["distance_km"] > 2
            and s["max_alt"] > s["min_alt"] + 200
            and s["duration_s"] > 600
        )
        assert s["airtime_s"] == f["landing"]["t"] - f["takeoff"]["t"] and s["max_speed"] < 50 and s["max_climb"] > 1
        west, south, east, north = f["bbox"]
        assert 7 < west < east < 9 and 46 < south < north < 47
        assert 5 <= len(f["preview"]) <= 500 and all(len(p) == 3 for p in f["preview"])
        assert abs(f["takeoff"]["lat"] - f["preview"][0][1]) < 0.01


def test_privacy_nothing_identifying_without_consent(client):
    text = client.get("/api/days/2026-07-15/flights").text
    assert "D-9999" not in text and "Y9" not in text  # the device that did not agree to be identified


def test_an_open_flight_is_marked_live(client):
    (f,) = client.get("/api/days/2026-07-16/flights").json()
    assert (f["live"], f["status"], f["close_reason"], f["landing"]) == (True, "active", None, None)
    assert f["preview"] is None  # the preview is made when the flight closes; the track endpoint has the points


def test_a_day_without_flights_is_an_empty_list(client):
    assert client.get("/api/days/2026-01-01/flights").json() == []
    assert client.get("/api/days/2026-07-15/flights", params={"region": "at"}).json() == []
    assert client.get("/api/days/2026-02-30/flights").status_code == 422
    assert client.get("/api/days/soon/flights").status_code == 422


# ---------------------------------------------------------------- one flight and its track


def test_one_flight(client):
    listed = client.get("/api/days/2026-07-15/flights").json()[0]
    assert client.get(f"/api/flights/{listed['id']}").json() == listed
    assert client.get("/api/flights/99999").status_code == 404
    assert client.get("/api/flights/abc").status_code == 422


def test_a_track_is_columns_in_real_units(client):
    flight = client.get("/api/days/2026-07-15/flights").json()[0]
    t = client.get(f"/api/flights/{flight['id']}/track").json()
    assert t["flight_id"] == flight["id"] and t["n"] == flight["stats"]["fix_count"]
    assert set(t) == {"flight_id", "n", "t", "lat", "lon", "alt", "gnd", "spd", "vs", "hdg"}
    assert all(len(t[k]) == t["n"] for k in ("t", "lat", "lon", "alt", "gnd", "spd", "vs", "hdg"))
    assert t["t"] == sorted(set(t["t"])) and t["t"][0] == flight["start"] and t["t"][-1] == flight["end"]
    assert (
        all(46 < v < 47 for v in t["lat"])
        and all(7 < v < 8 for v in t["lon"])
        and all(1000 < v < 3000 for v in t["alt"])
    )
    assert set(t["gnd"]) == {None}  # no terrain in this setup
    assert max(t["spd"]) < 50 and min(t["vs"]) > -6 and max(t["vs"]) < 6 and all(0 <= h < 360 for h in t["hdg"])
    assert t["lat"][0] == pytest.approx(flight["takeoff"]["lat"], abs=0.01)


def test_the_track_of_an_open_flight_includes_positions_not_yet_written(site, client):
    runtime = site["runtime"]
    (f,) = client.get("/api/days/2026-07-16/flights").json()
    before = client.get(f"/api/flights/{f['id']}/track").json()
    sim = site["sim"]
    for k in range(260, 275):  # fifteen more seconds, deliberately not flushed
        for line in sim.lines(sim.at(k)):
            runtime.consume(sim.at(k), line)
    assert len(runtime.tracker.unflushed(f["id"])) == 15
    during = client.get(f"/api/flights/{f['id']}/track").json()
    assert during["n"] == before["n"] + 15 and during["t"][-1] == before["t"][-1] + 15
    runtime.tracker.flush(force=True)
    after = client.get(f"/api/flights/{f['id']}/track").json()
    assert after["t"] == during["t"]  # the same points, now from the database: nothing doubled, nothing lost


# ---------------------------------------------------------------- transport details


def test_big_answers_are_compressed_small_ones_and_streams_are_not(client):
    flight = client.get("/api/days/2026-07-15/flights").json()[0]
    big = client.get(f"/api/flights/{flight['id']}/track", headers={"Accept-Encoding": "gzip"})
    assert big.headers["content-encoding"] == "gzip" and int(big.headers["content-length"]) < len(big.content) / 2
    assert big.json()["n"] == flight["stats"]["fix_count"]  # and it is the same data after decompression
    assert client.get("/api/health", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") is None
    with client.stream("GET", "/api/live/stream?limit=1", headers={"Accept-Encoding": "gzip"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream") and "content-encoding" not in r.headers
        r.read()


# ---------------------------------------------------------------- live


def test_live_is_a_full_snapshot(site, client):
    j = client.get("/api/live").json()
    assert j["full"] is True and j["removed"] == [] and j["seq"] > 0
    ids = {a["id"]: a for a in j["aircraft"]}
    assert set(ids) <= {"FLRD00004", "FLRD00001", "FLRD00002", "FNTD00003"}
    mine = ids["FLRD00004"]  # the pilot of the open flight
    assert (
        mine["flying"] and mine["flight_id"] and mine["takeoff"] and mine["src"] == "FLARM" and len(mine["trail"]) > 10
    )
    assert all(len(p) == 4 for p in mine["trail"]) and "pts" not in mine


def test_the_stream_sends_a_snapshot_then_deltas_with_every_new_position(site):
    runtime, sim = site["runtime"], site["sim"]
    quiet = create_app(runtime, stream_interval=0.05, full_every=1000)  # no snapshot interrupts the deltas

    def feed_while_reading() -> None:
        time.sleep(0.4)
        for k in range(275, 280):
            for line in sim.lines(sim.at(k)):
                runtime.consume(sim.at(k), line)
            time.sleep(0.02)

    feeder = threading.Thread(target=feed_while_reading)
    with serve(quiet) as (url, _), httpx.Client(base_url=url, timeout=20) as client:
        feeder.start()
        received = stream_messages(client, "/api/live/stream", 25)
        feeder.join()
    assert received[0]["full"] is True and not any(m["full"] for m in received[1:])
    points = [p for m in received[1:] for a in m["aircraft"] for p in a["pts"]]
    assert [p[0] for p in points] == [int(sim.at(k).timestamp()) for k in range(275, 280)]
    assert all(len(p) == 8 for p in points)  # [t, lon, lat, alt, speed, vario, heading, ground]
    seqs = [m["seq"] for m in received]
    assert seqs == sorted(seqs)


def test_a_full_snapshot_comes_round_again(client):
    received = stream_messages(client, "/api/live/stream", 30)  # 30 messages at 0.05 s, snapshots every 0.5 s
    fulls = [m for m in received if m["full"]]
    assert (
        len(fulls) >= 2 and received[0]["full"] and all(a["trail"] for m in fulls for a in m["aircraft"] if a["flying"])
    )


def test_stream_messages_are_server_sent_events(client):
    with client.stream("GET", "/api/live/stream?limit=2") as r:
        text = "".join(r.iter_text())
    blocks = [b for b in text.split("\n\n") if b]
    assert len(blocks) == 2 and all(b.startswith("id: ") and "\ndata: {" in b for b in blocks)
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert client.get("/api/live/stream?limit=0").status_code == 422


def test_a_viewer_who_goes_away_leaves_nothing_behind(site, client):
    server = site["server"]
    with client.stream("GET", "/api/live/stream") as r:  # no limit: it would run for ever
        it = r.iter_lines()
        assert any(line.startswith("data: ") for line in it)
        assert server.server_state.connections
    deadline = time.monotonic() + 5
    while server.server_state.connections and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not server.server_state.connections


def test_changes_after_a_known_position_arrive_as_removals(site, client):
    runtime = site["runtime"]
    cursor = runtime.tracker.live()["seq"]
    runtime.tracker.sweep(datetime(2030, 1, 1, tzinfo=UTC))  # far in the future: everybody has gone quiet
    removed = {i for m in stream_messages(client, "/api/live/stream", 1) for i in m["removed"]}
    assert removed == set()  # a full snapshot never lists removals ...
    assert runtime.tracker.live(since=cursor)["removed"]  # ... but a client that keeps up is told
