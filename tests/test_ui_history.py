"""History in a real browser: the calendar, a day's flights drawn on the map, picking one (shots: tests/shots)."""

from __future__ import annotations

import httpx
import pytest

from .uirig import camera_still, count_saturated, day_label, local_day, new_context, shot, wait_for_aircraft

pytestmark = pytest.mark.ui

TODAY, YESTERDAY, DAY_BEFORE = (local_day(n).isoformat() for n in (0, 1, 2))


def day_url(day: str, query: str = "") -> str:
    return f"/#/day/{day}{query}"


def open_day(make_page, day: str, query: str = "", **options):
    page = make_page(day_url(day, query), **options)
    page.wait_for_function("window.prack && window.prack.mode() === 'history' && !window.prack.dayView.loading")
    return page


def chips(page) -> list[str]:
    return page.locator(".flight .who").all_inner_texts()


def wait_previews(page, n: int) -> None:
    """Wait until the map is drawing n paths of the day."""
    page.wait_for_function(
        f"(() => {{ const l = window.prack.layer('previews'); return l !== null && l.props.data.length === {n}; }})()",
        timeout=30_000,
    )


def shaded(page) -> None:
    """Wait until the calendar has its shading (it arrives with the counts) and the colour transition is over."""
    page.wait_for_function("document.querySelector('.cal button[data-level=\"4\"]') !== null")
    page.wait_for_timeout(350)


def flights_of(app: str, day: str) -> list[dict]:
    return httpx.get(f"{app}/api/days/{day}/flights", timeout=20).json()


# ---------------------------------------------------------------- Live | History and the date controls


def test_history_draws_every_flight_of_the_day_coloured_by_altitude(make_page, app):
    page = open_day(make_page, YESTERDAY)
    wait_previews(page, 2)
    assert len(chips(page)) == 2
    assert page.locator(".status").inner_text() == "2 flights"
    assert page.locator(".date-btn").inner_text() == day_label(local_day(1))
    assert page.get_by_role("radio", name="History").get_attribute("aria-checked") == "true"
    assert page.get_by_role("button", name="Ground").is_hidden()  # a live-only chip
    assert page.locator(".dot").get_attribute("data-level") == "ok"  # the OGN link is still shown
    # real pixels: the day's paths are on the canvas, in the altitude colours (not the violet of live markers)
    camera_still(page)
    png = page.screenshot()
    paths = page.evaluate("window.prack.dayView.flights.map((f) => f.path[Math.floor(f.path.length / 2)])")
    assert all(count_saturated(png, page_xy(page, lon, lat)) > 6 for lon, lat, _ in paths)
    shot(page, "08-history-day")


def page_xy(page, lon: float, lat: float) -> tuple[float, float]:
    return tuple(page.evaluate("([lon, lat]) => window.prack.view.project(lon, lat, 0)", [lon, lat]))


def test_the_segmented_control_switches_between_live_and_history(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    assert page.locator(".daynav").is_hidden()  # only History has the day buttons
    page.get_by_role("radio", name="History").click()
    page.wait_for_function("window.prack.mode() === 'history'")
    assert page.evaluate("location.hash").startswith(f"#/day/{TODAY}")
    assert page.locator(".daynav").is_visible()
    page.wait_for_function("window.prack.layer('previews') !== null && window.prack.layer('markers') === null")
    page.get_by_role("radio", name="Live").click()
    page.wait_for_function("window.prack.mode() === 'live' && window.prack.layer('markers') !== null")
    assert page.evaluate("location.hash").startswith("#/live")
    assert page.locator(".flights").is_hidden()
    page.go_back()  # the back button returns to History
    page.wait_for_function("window.prack.mode() === 'history'")
    page.get_by_role("radio", name="History").click()  # idempotent
    assert page.evaluate("location.hash").startswith(f"#/day/{TODAY}")


def test_today_shows_the_flights_still_in_the_air_with_paths_fetched_from_their_tracks(make_page):
    page = open_day(make_page, TODAY)
    assert len(chips(page)) == 4  # the four in the air; the one on the ground is no flight
    page.wait_for_function("window.prack.layer('previews') && window.prack.layer('previews').props.data.length === 4")
    # the day list has no stored preview for them yet: the path came from the track endpoint
    assert page.evaluate(
        "window.prack.dayView.flights.every((f) => f.live && f.preview === null && f.path.length > 50)"
    )


def test_the_day_buttons_step_through_the_days(make_page):
    page = open_day(make_page, YESTERDAY)
    page.get_by_role("button", name="Previous day").click()
    page.wait_for_function(f"location.hash.startsWith('#/day/{DAY_BEFORE}')")
    page.wait_for_function("window.prack.dayView.flights.length === 3 && !window.prack.dayView.loading")
    wait_previews(page, 3)
    assert len(chips(page)) == 3
    page.get_by_role("button", name="Next day").click()
    page.wait_for_function(f"location.hash.startsWith('#/day/{YESTERDAY}')")
    page.get_by_role("button", name="Next day").click()
    page.wait_for_function(f"location.hash.startsWith('#/day/{TODAY}')")
    assert page.get_by_role("button", name="Next day").is_disabled()  # there is no tomorrow
    assert page.get_by_role("button", name="Today").is_disabled()
    page.get_by_role("button", name="Previous day").click()
    page.get_by_role("button", name="Today").click()
    page.wait_for_function(f"location.hash.startsWith('#/day/{TODAY}')")
    page.go_back()  # every day is a history entry
    page.wait_for_function(f"location.hash.startsWith('#/day/{YESTERDAY}')")
    page.wait_for_function(
        "window.prack.dayView.day === '" + YESTERDAY + "' && window.prack.dayView.flights.length === 2"
    )


# ---------------------------------------------------------------- the calendar


def test_the_calendar_shades_the_days_by_their_flights_and_picks_a_day(make_page):
    page = open_day(make_page, TODAY)
    button = page.locator(".date-btn")
    assert button.get_attribute("aria-expanded") == "false"
    button.click()
    cal = page.locator(".cal")
    cal.wait_for(state="visible")
    assert button.get_attribute("aria-expanded") == "true"
    day = lambda d: cal.locator(f'button[data-day="{d}"]')  # noqa: E731
    page.wait_for_function(f"document.querySelector('.cal button[data-day=\"{DAY_BEFORE}\"]').dataset.level !== '0'")
    levels = {d: int(day(d).get_attribute("data-level")) for d in (TODAY, YESTERDAY, DAY_BEFORE)}
    assert levels[DAY_BEFORE] == 4 or levels[TODAY] == 4  # the busiest of the three is the darkest
    assert levels[YESTERDAY] > 0 and levels[DAY_BEFORE] > 0 and levels[TODAY] > 0
    assert levels[DAY_BEFORE] > levels[YESTERDAY] or levels[DAY_BEFORE] == 4  # 3 flights against 2
    assert "3 flights" in day(DAY_BEFORE).get_attribute("aria-label")
    assert day(TODAY).get_attribute("aria-current") == "date" and day(TODAY).get_attribute("aria-pressed") == "true"
    assert day(local_day(0).replace(day=1).isoformat()).get_attribute("data-level") in ("0", "1", "2", "3", "4")
    disabled = cal.locator("button[data-day]:disabled").evaluate_all("(bs) => bs.map((b) => b.dataset.day)")
    assert all(d > TODAY for d in disabled)  # only days after today cannot be picked
    page.wait_for_timeout(350)
    shot(page, "09-calendar")
    day(YESTERDAY).click()
    cal.wait_for(state="hidden")
    page.wait_for_function(f"location.hash.startsWith('#/day/{YESTERDAY}')")
    page.wait_for_function("window.prack.dayView.flights.length === 2 && !window.prack.dayView.loading")
    assert page.locator(".date-btn").inner_text() == day_label(local_day(1))
    assert button.evaluate("(b) => document.activeElement === b")  # the focus goes back to the button


def test_the_calendar_works_with_the_keyboard(make_page):
    page = open_day(make_page, YESTERDAY)
    button = page.locator(".date-btn")
    button.focus()
    page.keyboard.press("Enter")
    cal = page.locator(".cal")
    cal.wait_for(state="visible")
    focused = "document.activeElement.dataset.day"
    assert page.evaluate(focused) == YESTERDAY  # the selected day has the focus
    page.keyboard.press("ArrowLeft")
    assert page.evaluate(focused) == DAY_BEFORE
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert page.evaluate(focused) == TODAY
    page.keyboard.press("ArrowRight")  # not into the future
    assert page.evaluate(focused) == TODAY
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Enter")
    cal.wait_for(state="hidden")
    page.wait_for_function(f"location.hash.startsWith('#/day/{DAY_BEFORE}')")
    button.press("Enter")
    cal.wait_for(state="visible")
    page.keyboard.press("Escape")
    cal.wait_for(state="hidden")
    assert button.evaluate("(b) => document.activeElement === b")
    assert "sel=" not in page.evaluate("location.hash")  # Escape closed the calendar, it did not deselect anything


def test_the_calendar_pages_through_the_months_and_closes_when_clicked_away(make_page):
    page = open_day(make_page, TODAY)
    page.locator(".date-btn").click()
    cal = page.locator(".cal")
    title = cal.locator(".cal-title")
    here = title.inner_text()
    assert cal.get_by_role("button", name="Next month").is_disabled()  # no months in the future
    cal.get_by_role("button", name="Previous month").click()
    assert title.inner_text() != here
    cal.get_by_role("button", name="Next month").click()
    assert title.inner_text() == here
    page.mouse.click(700, 500)  # on the map
    cal.wait_for(state="hidden")
    assert "sel=" not in page.evaluate("location.hash")


def test_the_calendar_of_live_opens_on_today_and_picking_a_day_goes_to_history(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    page.locator(".date-btn").click()
    cal = page.locator(".cal")
    cal.wait_for(state="visible")
    cal.locator(f'button[data-day="{DAY_BEFORE}"]').click()
    page.wait_for_function("window.prack.mode() === 'history'")
    assert page.evaluate("location.hash").startswith(f"#/day/{DAY_BEFORE}")
    page.wait_for_function("window.prack.dayView.flights.length === 3 && !window.prack.dayView.loading")


# ---------------------------------------------------------------- picking a flight


def test_clicking_a_path_selects_the_flight_and_shows_its_card_and_whole_track(make_page, app):
    page = open_day(make_page, DAY_BEFORE)
    flights = flights_of(app, DAY_BEFORE)
    target = flights[1]
    camera_still(page)
    lon, lat, _ = target["preview"][len(target["preview"]) // 2]
    x, y = page_xy(page, lon, lat)
    page.mouse.move(x, y)
    page.wait_for_function("document.querySelector('.hint') && !document.querySelector('.hint').hidden")  # who is this?
    assert target["label"] in page.locator(".hint").inner_text()
    page.mouse.click(x, y)
    card = page.locator(".card")
    card.wait_for(state="visible")
    assert card.locator(".name").inner_text() == target["label"]
    assert f"sel={target['id']}" in page.evaluate("location.hash")
    page.wait_for_function("window.prack.track.loaded && window.prack.layer('track') !== null")
    st = target["stats"]
    assert card.locator("[data-k=maxalt]").inner_text().replace(" ", "") == f"{round(st['max_alt'])}m"
    assert card.locator("[data-k=distance]").inner_text().replace(" ", "") == f"{st['distance_km']:.1f}km"
    for key in ("maxalt", "gain", "climb", "takeoff", "duration", "distance"):
        assert card.locator(f"[data-k={key}]").inner_text() not in ("", "–"), key
    assert page.get_by_role("button", name="Follow").is_hidden()  # nothing to follow in History
    assert page.locator(".flight[aria-pressed=true] .who").inner_text() == target["label"]
    assert page.evaluate("window.prack.layer('previews').props.data.length") == len(flights) - 1  # others stay, dimmed
    assert page.evaluate("window.prack.layer('track-ends') !== null")  # take-off and landing
    assert page.locator(".hint").is_hidden()  # the card says who it is; no tooltip on top of it
    camera_still(page)
    shot(page, "10-history-selected")

    page.keyboard.press("Escape")
    card.wait_for(state="hidden")
    assert "sel=" not in page.evaluate("location.hash")
    page.wait_for_function(f"window.prack.layer('previews').props.data.length === {len(flights)}")
    page.mouse.click(40, 700)  # an empty spot selects nothing and deselects
    assert card.is_hidden()


def test_a_chip_selects_a_flight_and_zooms_to_it(make_page, app):
    page = open_day(make_page, YESTERDAY)
    flights = flights_of(app, YESTERDAY)
    page.locator(".flight").nth(0).click()
    page.locator(".card").wait_for(state="visible")
    assert page.locator(".card .name").inner_text() == flights[0]["label"]
    page.wait_for_function("window.prack.track.loaded && !window.prack.view.map.isMoving()")
    west, south, east, north = flights[0]["bbox"]
    inside = page.evaluate(
        "([w, s, e, n]) => { const b = window.prack.view.map.getBounds();"
        " return b.getWest() <= w && b.getSouth() <= s && b.getEast() >= e && b.getNorth() >= n; }",
        [west, south, east, north],
    )
    assert inside  # the whole flight is in view
    page.locator(".flight").nth(1).click()
    page.wait_for_function(f"location.hash.includes('sel={flights[1]['id']}')")
    assert page.locator(".card .name").inner_text() == flights[1]["label"]
    page.get_by_role("button", name="Zoom to the whole flight").click()  # and the card's own button does it again


def test_the_selection_survives_switching_to_3d_and_back(make_page, app):
    page = open_day(make_page, DAY_BEFORE)
    flight = flights_of(app, DAY_BEFORE)[0]
    page.locator(".flight").nth(0).click()
    page.wait_for_function("window.prack.track.loaded")
    page.get_by_role("radio", name="3D").click()
    page.wait_for_function("window.prack.view.map.getPitch() === 60", timeout=30_000)
    page.wait_for_function("window.prack.layer('track-curtain') !== null")
    z = page.evaluate("window.prack.layer('previews').props.data.attributes.getPath.value[2]")
    assert z > 500  # the other flights are drawn at altitude too
    assert page.locator(".card .name").inner_text() == flight["label"]
    shot(page, "11-history-3d")
    page.get_by_role("radio", name="2D").click()
    page.wait_for_function("window.prack.view.map.getPitch() === 0", timeout=30_000)
    assert f"sel={flight['id']}" in page.evaluate("location.hash")
    assert "view=3d" not in page.evaluate("location.hash")


# ---------------------------------------------------------------- the address bar


def test_a_day_with_a_selection_and_the_3d_view_comes_back_from_the_url(make_page, app):
    flight = flights_of(app, YESTERDAY)[1]
    page = make_page(day_url(YESTERDAY, f"?sel={flight['id']}&view=3d"))
    page.locator(".card").wait_for(state="visible", timeout=60_000)
    assert page.locator(".card .name").inner_text() == flight["label"]
    page.wait_for_function("window.prack.view.mode3d === true && window.prack.track.loaded")
    assert page.locator(".date-btn").inner_text() == day_label(local_day(1))


def test_a_flight_that_is_not_on_that_day_is_dropped_with_a_message(make_page, app):
    other = flights_of(app, DAY_BEFORE)[0]["id"]
    page = make_page(day_url(YESTERDAY, f"?sel={other}"))
    page.wait_for_function("document.querySelector('.toast').textContent.includes('not on this day')")
    assert "sel=" not in page.evaluate("location.hash")
    assert page.locator(".card").is_hidden()


def test_back_and_forward_move_between_selections_and_days(make_page, app):
    flights = flights_of(app, YESTERDAY)
    page = open_day(make_page, YESTERDAY)
    page.evaluate(f"window.prack.selectFlight({flights[0]['id']})")
    page.locator(".card").wait_for(state="visible")
    page.get_by_role("button", name="Previous day").click()
    page.wait_for_function(f"location.hash.startsWith('#/day/{DAY_BEFORE}')")
    assert page.locator(".card").is_hidden()
    page.go_back()  # to yesterday, with the flight still selected as it was left
    page.wait_for_function(f"window.prack.dayView.day === '{YESTERDAY}' && window.prack.dayView.flights.length === 2")
    page.locator(".card").wait_for(state="visible")
    assert page.locator(".card .name").inner_text() == flights[0]["label"]
    assert f"sel={flights[0]['id']}" in page.evaluate("location.hash")
    page.go_back()  # and before the selection
    page.locator(".card").wait_for(state="hidden")
    assert "sel=" not in page.evaluate("location.hash")
    page.go_forward()
    page.locator(".card").wait_for(state="visible")
    assert page.locator(".card .name").inner_text() == flights[0]["label"]


def test_an_invalid_day_in_the_url_falls_back_to_live(make_page):
    page = make_page("/#/day/2026-02-30")
    wait_for_aircraft(page, 5)
    assert page.evaluate("window.prack.mode()") == "live"


# ---------------------------------------------------------------- phone and themes


def test_the_phone_layout_of_history_has_a_two_row_bar_a_calendar_that_fits_and_big_targets(make_page):
    page = open_day(make_page, YESTERDAY, size=(390, 844), touch=True)
    bar = page.locator(".bar").bounding_box()
    assert bar["height"] < 130 and bar["x"] + bar["width"] <= 390  # two rows, nothing sticking out
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.locator(".date-btn").click()
    cal = page.locator(".cal")
    cal.wait_for(state="visible")
    box = cal.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    assert box["y"] >= bar["y"] + bar["height"]  # under the whole bar
    small = page.evaluate(
        """Array.from(document.querySelectorAll('#ui button:not(:disabled), .maplibregl-ctrl-group button'))
             .filter((b) => b.offsetParent !== null)
             .map((b) => {
               const r = b.getBoundingClientRect();
               return [b.getAttribute('aria-label') || b.textContent, r.width, r.height];
             })
             .filter(([, w, h]) => w < 43.5 || h < 43.5)"""
    )
    assert small == []
    shaded(page)
    shot(page, "12-history-phone-calendar")
    page.locator(f'.cal button[data-day="{YESTERDAY}"]').click()
    page.locator(".flight").nth(0).click()
    page.locator(".card").wait_for(state="visible")
    card = page.locator(".card").bounding_box()
    assert card["x"] == 0 and card["width"] == 390 and abs(card["y"] + card["height"] - 844) < 1.5
    assert (
        page.locator(".dock").is_hidden() or page.locator(".flights").is_hidden()
    )  # the sheet takes the strip's place
    shot(page, "13-history-phone-selected")


def test_the_light_theme_in_history(make_page):
    page = open_day(make_page, YESTERDAY, theme="light")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    camera_still(page)
    page.locator(".date-btn").click()
    page.locator(".cal").wait_for(state="visible")
    shaded(page)
    shot(page, "14-history-light")


def test_a_failing_day_request_is_reported_and_does_not_break_the_page(chromium, app):
    context = new_context(chromium, app)
    page = context.new_page()
    page.route("**/api/days/*/flights", lambda route: route.fulfill(status=500, body="boom"))
    page.goto(app + day_url(YESTERDAY))
    page.wait_for_function(
        "document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('Could not load')"
    )
    assert page.locator(".status").inner_text() == "Could not load the flights"
    page.get_by_role("radio", name="Live").click()  # and Live still works
    page.wait_for_function("window.prack.mode() === 'live'")
    context.close()
