"""Unit tests for the drawing primitives in server.py.

Run with:  .venv/bin/python -m pytest
"""

import json

from server import PixelCanvas, draw_smiley, parse_hex


def test_parse_hex():
    assert parse_hex("#ff8000") == (255, 128, 0)
    assert parse_hex("ff8000") == (255, 128, 0)


def test_parse_hex_rejects_bad_input():
    for bad in ("#12345", "#gggggg", "", "#1234567", "12345"):
        try:
            parse_hex(bad)
            assert False, f"{bad!r} should have raised"
        except ValueError:
            pass


def test_set_pixel_reports_changed_cell():
    c = PixelCanvas(4, 4)
    changes = c.set_pixel(1, 1, (255, 0, 0))
    assert len(changes) == 1
    ch = changes[0]
    assert (ch.x, ch.y, ch.color) == (1, 1, (255, 0, 0))
    # setting the same color again reports no change
    assert c.set_pixel(1, 1, (255, 0, 0)) == []


def test_set_pixel_out_of_bounds():
    c = PixelCanvas(4, 4)
    try:
        c.set_pixel(4, 0, (1, 2, 3))
        assert False, "should have raised"
    except ValueError:
        pass


def test_fill_rect_clips():
    c = PixelCanvas(10, 10)
    changes = c.fill_rect(-5, -5, 10, 10, (1, 2, 3))
    assert len(changes) == 25  # x 0..4, y 0..4
    assert c.get(0, 0) == (1, 2, 3)
    assert c.get(4, 4) == (1, 2, 3)


def test_draw_line_diagonal():
    c = PixelCanvas(5, 5)
    changes = c.draw_line(0, 0, 4, 4, (9, 9, 9))
    points = {(ch.x, ch.y) for ch in changes}
    assert points == {(i, i) for i in range(5)}


def test_draw_line_clips():
    c = PixelCanvas(5, 5)
    changes = c.draw_line(-5, 2, 9, 2, (7, 7, 7))
    assert len(changes) == 5  # every on-canvas pixel of the horizontal line


def test_clear_returns_only_actual_diffs():
    c = PixelCanvas(3, 3)
    c.set_pixel(1, 1, (255, 0, 0))
    changes = c.clear((0, 0, 0))
    assert len(changes) == 1
    assert c.background == (0, 0, 0)
    assert c.get(1, 1) == (0, 0, 0)
    assert c.clear((0, 0, 0)) == []


def test_snapshot_is_json_serializable():
    c = PixelCanvas(2, 2, (1, 2, 3))
    c.set_pixel(0, 0, (255, 0, 0))
    data = c.snapshot()
    assert data["width"] == 2
    assert data["height"] == 2
    assert data["background"] == [1, 2, 3]
    assert data["pixels"][0][0] == [255, 0, 0]
    json.dumps(data)  # must not raise


def test_smiley_draws_on_default_canvas():
    c = PixelCanvas(32, 32)
    changes = draw_smiley(c)
    assert len(changes) > 50
    assert c.get(16, 16) == (255, 214, 64)  # face centre is yellow
