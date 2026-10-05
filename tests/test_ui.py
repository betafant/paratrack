"""The live map in a real browser: markers, labels, selection, 2D/3D, themes, phone layout (shots: tests/shots)."""

from __future__ import annotations

import pytest

from .uirig import (
    count_pixels,
    day_label,
    jump,
    label_texts,
    local_day,
    new_context,
    screen_position,
    shot,
    shown,
    wait_for_aircraft,
)

pytestmark = pytest.mark.ui

MIA = "D00002"  # a FLARM + FANET pilot called Mia


# ---------------------------------------------------------------- the page and the markers


def test_the_page_loads_cleanly_and_shows_the_paragliders_in_the_air(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    assert page.title() == "prack"
    page.wait_for_function("document.querySelector('#air-count').textContent.startsWith('4 paragliders')")
    assert page.locator(".dot").get_attribute("data-level") == "ok"  # the OGN link is up
    assert page.locator(".dot").get_attribute("aria-label") == "Connected to the OGN feed"
    shot(page, "01-live-switzerland")


def test_markers_are_really_drawn_on_the_map(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    jump(page, MIA, 12)
    png = page.screenshot()
    assert count_pixels(png, screen_position(page, MIA)) > 30  # the violet arrow, not an empty canvas
    assert count_pixels(png, (5, 5)) == 0
    shot(page, "02-live-zoomed")


def test_labels_follow_the_zoom(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    jump(page, MIA, 7.4)
    assert label_texts(page) == []  # far out: markers only
    jump(page, MIA, 9.6)
    page.wait_for_function("window.prack.layer('labels') !== null")
    far = label_texts(page)
    assert far and all(" · " not in t for t in far) and "Mia" in far  # names
    jump(page, MIA, 12.5)
    near = label_texts(page)
    mia = next(t for t in near if t.startswith("Mia"))
    assert " · " in mia and " m" in mia  # "Mia · 1 587 m · −1.9": name, altitude, vario
    assert mia.split(" · ")[2][0] in "+−" or mia.split(" · ")[2] == "0.0"


def test_grounded_paragliders_are_hidden_until_the_chip_is_on(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    chip = page.get_by_role("button", name="Ground")
    assert chip.get_attribute("aria-pressed") == "false"
    assert chip.locator(".count").inner_text() == "1"
    jump(page, MIA, 9)
    assert shown(page, "markers") == 4
    chip.click()
    assert chip.get_attribute("aria-pressed") == "true"
    page.wait_for_function("window.prack.layer('markers').props.data.length === 5")
    chip.click()
    page.wait_for_function("window.prack.layer('markers').props.data.length === 4")


# ---------------------------------------------------------------- selection


def test_clicking_a_marker_selects_it_and_draws_the_whole_flight(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    jump(page, MIA, 12)
    x, y = screen_position(page, MIA)
    page.mouse.move(x, y)
    page.wait_for_timeout(300)
    page.mouse.click(x, y)
    card = page.locator(".card")
    card.wait_for(state="visible")
    assert card.locator(".name").inner_text() == "Mia"
    assert card.locator(".tag").inner_text() == "FLARM"  # FLARM wins over FANET, the pilot name stays
    assert "sel=D00002" in page.evaluate("location.hash")
    page.wait_for_function("window.prack.track.loaded && window.prack.track.length > 100")
    page.wait_for_function("window.prack.layer('track') !== null")
    for cell in ("alt", "agl", "spd", "vs", "hdg", "takeoff", "duration", "distance"):
        assert card.locator(f"[data-k={cell}]").inner_text() not in ("", "–"), cell
    assert page.evaluate("window.prack.layer('selection-ring') !== null")
    shot(page, "03-selected-2d")

    page.keyboard.press("Escape")
    card.wait_for(state="hidden")
    assert "sel=" not in page.evaluate("location.hash")
    page.wait_for_function("window.prack.layer('track') === null")


def test_the_back_button_undoes_a_selection(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    page.evaluate(f"window.prack.select('{MIA}')")
    page.locator(".card").wait_for(state="visible")
    page.go_back()
    page.locator(".card").wait_for(state="hidden")
    page.evaluate("window.prack.view.map.jumpTo({zoom: 10})")  # moving the camera must not bring the selection back
    page.wait_for_function("location.hash.includes('z=10')")
    assert "sel=" not in page.evaluate("location.hash")
    page.go_forward()
    page.locator(".card").wait_for(state="visible")
    assert page.locator(".card .name").inner_text() == "Mia"


def test_the_card_shows_what_the_stream_knows_and_follows_the_aircraft(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    page.evaluate(f"window.prack.select('{MIA}')")
    page.locator(".card").wait_for(state="visible")
    follow = page.get_by_role("button", name="Follow")
    follow.click()
    assert follow.get_attribute("aria-pressed") == "true"
    # the map moves to the aircraft and stays on it
    page.wait_for_function(
        "(() => { const p = window.prack; const e = p.store.find('D00002'); const c = p.view.map.getCenter();"
        " return Math.abs(c.lng - e.lon) < 1e-3 && Math.abs(c.lat - e.lat) < 1e-3; })()"
    )
    page.mouse.move(600, 400)
    page.mouse.down()
    page.mouse.move(700, 450, steps=5)
    page.mouse.up()
    assert follow.get_attribute("aria-pressed") == "false"  # dragging the map ends it


def test_a_selection_in_the_url_is_restored_and_an_unknown_one_is_dropped(make_page):
    page = make_page(f"/#/live?sel={MIA}&view=3d")
    page.locator(".card").wait_for(state="visible", timeout=60_000)
    assert page.locator(".card .name").inner_text() == "Mia"
    page.wait_for_function("window.prack.view.mode3d === true")
    other = make_page("/#/live?sel=ZZZZZZ")
    wait_for_aircraft(other, 5)
    other.wait_for_function("document.querySelector('.toast').textContent.includes('not in the air')")
    assert "sel=" not in other.evaluate("location.hash")
    assert other.locator(".card").is_hidden()


# ---------------------------------------------------------------- 2D / 3D


def test_the_3d_view_shows_terrain_and_the_track_at_altitude(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    jump(page, MIA, 11.5)
    three_d = page.get_by_role("radio", name="3D")
    flat = page.get_by_role("radio", name="2D")
    assert flat.get_attribute("aria-checked") == "true" and three_d.is_enabled()
    three_d.click()
    page.wait_for_function("window.prack.view.map.getPitch() === 60", timeout=30_000)
    assert three_d.get_attribute("aria-checked") == "true"
    assert page.evaluate("window.prack.view.map.getTerrain() !== null && window.prack.view.mode3d")
    assert "view=3d" in page.evaluate("location.hash")
    page.wait_for_function(  # the terrain tiles arrived: the height of the ground below the camera is known
        "window.prack.view.map.queryTerrainElevation(window.prack.view.map.getCenter()) > 500",
        timeout=60_000,
    )
    height = page.evaluate("window.prack.view.map.queryTerrainElevation(window.prack.view.map.getCenter())")
    assert 700 < height < 1900  # the synthetic hills: 1300 m +- 500 m

    page.evaluate(f"window.prack.select('{MIA}')")
    page.wait_for_function("window.prack.track.length > 100 && window.prack.layer('track') !== null")
    path = page.evaluate("Array.from(window.prack.layer('track').props.data.attributes.getPath.value.slice(0, 3))")
    assert path[2] > 500  # the track is drawn at GPS altitude above sea level, not flat
    page.wait_for_function("window.prack.layer('track-curtain') !== null")  # and a faint curtain down to the ground
    page.wait_for_timeout(1500)
    shot(page, "04-selected-3d")

    flat.click()
    page.wait_for_function("window.prack.view.map.getPitch() === 0", timeout=30_000)
    page.wait_for_function("window.prack.view.map.getTerrain() === null")
    page.wait_for_function("window.prack.layer('track').props.data.attributes.getPath.value[2] === 0")
    flat_path = page.evaluate("Array.from(window.prack.layer('track').props.data.attributes.getPath.value.slice(0, 3))")
    assert flat_path[2] == 0
    page.wait_for_function("window.prack.layer('track-curtain') === null")
    assert "view=3d" not in page.evaluate("location.hash")


def test_resizing_the_window_keeps_the_overlay_on_the_map(make_page):
    """deck.gl in interleaved mode draws shifted after a resize unless its framebuffer follows the canvas."""
    page = make_page()
    wait_for_aircraft(page, 5)
    jump(page, MIA, 13)
    for size in ({"width": 900, "height": 600}, {"width": 1100, "height": 720}, {"width": 700, "height": 900}):
        page.set_viewport_size(size)
        page.wait_for_timeout(1500)
        sizes = page.evaluate(
            "(() => { const p = window.prack; const canvas = p.view.map.getCanvas();"
            " const fb = p.view.overlay._deck.device.canvasContext.getCurrentFramebuffer();"
            " return [fb.width, fb.height, canvas.width, canvas.height]; })()"
        )
        assert sizes[:2] == sizes[2:], sizes
        around = screen_position(page, MIA)
        assert count_pixels(page.screenshot(), around, radius=30) > 30  # the marker is where the map says it is


def test_the_camera_may_not_end_up_inside_a_mountain(make_page):
    page = make_page("/#/live?view=3d")
    wait_for_aircraft(page, 5)
    page.wait_for_function("window.prack.view.map.getPitch() === 60", timeout=30_000)
    page.evaluate(f"window.prack.select('{MIA}')")
    page.wait_for_function("window.prack.track.bounds !== null")
    page.evaluate("window.prack.view.fitBounds([7.0, 46.0, 7.01, 46.01])")  # a tiny box: a plain fit would zoom to 17
    page.wait_for_function("window.prack.view.map.getZoom() <= 12.01 && !window.prack.view.map.isMoving()")
    assert page.evaluate("window.prack.view.map.getPitch()") == 60


# ---------------------------------------------------------------- themes, layout, input


def test_the_theme_toggle_and_the_system_preference(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"  # dark by default
    shot(page, "05-dark")
    page.get_by_role("button", name="Switch to the light theme").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.reload()
    wait_for_aircraft(page, 5)
    assert page.evaluate("document.documentElement.dataset.theme") == "light"  # remembered
    shot(page, "06-light")
    page.get_by_role("button", name="Switch to the dark theme").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    system_light = make_page(theme="light")  # a visitor whose system prefers light, with nothing stored yet
    assert system_light.evaluate("document.documentElement.dataset.theme") == "light"


def test_the_base_map_can_be_switched(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    button = page.get_by_role("button", name="Base map: Grey")
    button.click()
    assert page.evaluate("window.prack.view.map.getLayoutProperty('base-swisstopo-colour', 'visibility')") == "visible"
    assert page.evaluate("window.prack.view.map.getLayoutProperty('base-swisstopo-grey', 'visibility')") == "none"
    page.get_by_role("button", name="Base map: Colour").click()
    page.get_by_role("button", name="Base map: Aerial").click()
    page.get_by_role("button", name="Base map: Grey").wait_for()  # and round again


def test_a_phone_gets_a_bottom_sheet_and_big_touch_targets(make_page):
    page = make_page(f"/#/live?sel={MIA}", size=(390, 844), touch=True)
    page.locator(".card").wait_for(state="visible", timeout=60_000)
    wait_for_aircraft(page, 5)
    card = page.locator(".card").bounding_box()
    assert card["x"] == 0 and card["width"] == 390 and abs(card["y"] + card["height"] - 844) < 1.5
    assert page.evaluate("document.documentElement.scrollWidth") <= 390  # nothing sticks out sideways
    bar = page.locator(".bar").bounding_box()
    assert bar["x"] >= 0 and bar["x"] + bar["width"] <= 390
    small = page.evaluate(
        """Array.from(document.querySelectorAll('#ui button, .maplibregl-ctrl-group button'))
             .filter((b) => b.offsetParent !== null)
             .map((b) => {
               const r = b.getBoundingClientRect();
               return [b.getAttribute('aria-label') || b.textContent, r.width, r.height];
             })
             .filter(([, w, h]) => w < 43.5 || h < 43.5)"""
    )
    assert small == []
    shot(page, "07-phone")


def test_the_controls_can_be_reached_with_the_keyboard(make_page):
    page = make_page()
    wait_for_aircraft(page, 5)
    focused = []
    for _ in range(4):
        page.keyboard.press("Tab")
        focused.append(
            page.evaluate(
                "(() => { const e = document.activeElement;"
                " return [e.textContent.trim(), getComputedStyle(e).outlineStyle]; })()"
            )
        )
    names = [name for name, _ in focused]
    assert names[0] == "Live" and names[2:] == ["2D", "Ground1"]  # the bar comes first
    assert names[1] == day_label(local_day(0))  # then the date button
    assert all(outline == "solid" for _, outline in focused)  # with a visible focus ring everywhere
    page.keyboard.press("Space")  # the Ground chip, pressed from the keyboard
    assert page.get_by_role("button", name="Ground").get_attribute("aria-pressed") == "true"
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("ArrowRight")  # the 2D | 3D control moves with the arrow keys
    page.wait_for_function("window.prack.view.mode3d === true", timeout=30_000)


def test_reduced_motion_makes_the_camera_jump(make_page):
    page = make_page()
    page.emulate_media(reduced_motion="reduce")
    page.reload()
    wait_for_aircraft(page, 5)
    page.get_by_role("radio", name="3D").click()
    page.wait_for_function("window.prack.view.map.getPitch() === 60", timeout=3000)


def test_a_broken_stream_turns_the_dot_red(chromium, app):
    context = new_context(chromium, app)
    page = context.new_page()
    page.route("**/api/live/stream", lambda route: route.abort())
    page.goto(app + "/")
    page.wait_for_function("document.querySelector('.dot').dataset.level === 'bad'", timeout=30_000)
    assert "No connection" in page.locator(".dot").get_attribute("aria-label")
    context.close()
