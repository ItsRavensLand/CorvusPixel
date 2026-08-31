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


# --------------------------------------------------------------------------- #
# Session-scoped socket path
# --------------------------------------------------------------------------- #


def test_default_socket_path_is_scoped_by_parent_pid(monkeypatch):
    import server as server_mod

    monkeypatch.delenv("CORVUSPIXEL_SOCK", raising=False)
    monkeypatch.setattr(server_mod.os, "getppid", lambda: 4242)
    assert server_mod.default_socket_path() == "/tmp/corvuspixel-4242.sock"


def test_default_socket_path_honours_env_override(monkeypatch):
    import server as server_mod

    monkeypatch.setenv("CORVUSPIXEL_SOCK", "/tmp/custom.sock")
    assert server_mod.default_socket_path() == "/tmp/custom.sock"


# --------------------------------------------------------------------------- #
# see_canvas: compact view
# --------------------------------------------------------------------------- #


def test_see_canvas_compact_view():
    from server import compact_view

    c = PixelCanvas(4, 4)
    for y in range(2):
        for x in range(2):
            c.set_pixel(x, y, (255, 0, 0))
    out = compact_view(c)
    assert ". = background #000000" in out
    assert "a = #ff0000" in out
    grid_lines = out.split("\n")[2:]  # first two lines are header + legend
    assert grid_lines[0].startswith("aa")  # top-left block is red
    assert len(out) < 500  # token-cheap


def test_see_canvas_downsamples_large_canvas():
    from server import compact_view

    c = PixelCanvas(64, 64)
    c.fill_rect(0, 0, 64, 64, (1, 2, 3))
    out = compact_view(c)
    grid = out.split("\n")[2:]
    assert all(len(row) <= 16 for row in grid)  # downsampled to 16 columns
    assert len(grid) <= 16
    assert len(out) < 2000


# --------------------------------------------------------------------------- #
# open_canvas: window launcher
# --------------------------------------------------------------------------- #


def test_launch_canvas_window_picks_first_available_terminal(monkeypatch):
    import server as server_mod

    monkeypatch.setattr(server_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(server_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured = {}

    class FakeProc:
        pid = 999

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(server_mod.subprocess, "Popen", fake_popen)
    launched = server_mod._launch_canvas_window("/tmp/x.sock")
    assert launched is not None
    assert launched.terminal == "x-terminal-emulator"
    assert launched.pid == 999
    assert captured["argv"][0] == "/usr/bin/x-terminal-emulator"
    assert "canvas_app.py" in " ".join(captured["argv"])
    assert "/tmp/x.sock" in " ".join(captured["argv"])


def test_launch_canvas_window_returns_none_without_terminal(monkeypatch):
    import server as server_mod

    monkeypatch.setattr(server_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(server_mod.shutil, "which", lambda name: None)
    assert server_mod._launch_canvas_window("/tmp/x.sock") is None
