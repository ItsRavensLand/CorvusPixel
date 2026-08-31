"""Tests for the interactive canvas app's input handling.

These drive the input handlers directly with synthetic bytes/events — no real
terminal needed. They verify that keyboard and mouse input produce the correct
pixel writes on the app's copy of the canvas.

Run with:  .venv/bin/python -m pytest
"""

import io

from rich.console import Console

from canvas_app import CanvasApp, PALETTE

FULL = {
    "type": "full",
    "width": 4,
    "height": 4,
    "background": [10, 10, 10],
    "pixels": [[[10, 10, 10]] * 4 for _ in range(4)],
}


def make_app() -> CanvasApp:
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=80, height=40, force_interactive=True, legacy_windows=False,
    )
    app = CanvasApp(
        "/tmp/nonexistent.sock", background=(10, 10, 10),
        console=console, input_stream=io.StringIO(),
    )
    app._apply(FULL)  # set the canvas + dimensions, like the first socket message
    return app


# --------------------------------------------------------------------------- #
# Keyboard: painting
# --------------------------------------------------------------------------- #


def test_space_paints_at_cursor():
    app = make_app()
    app._color = (255, 0, 0)
    changes = app._handle_char(" ")
    assert changes == [{"x": 0, "y": 0, "color": [255, 0, 0]}]
    assert app._pixels[0][0] == (255, 0, 0)


def test_space_toggles_back_to_background():
    app = make_app()
    app._color = (255, 0, 0)
    app._handle_char(" ")
    changes = app._handle_char(" ")
    assert changes == [{"x": 0, "y": 0, "color": [10, 10, 10]}]
    assert app._pixels[0][0] == (10, 10, 10)


def test_x_erases_to_background():
    app = make_app()
    app._pixels[2][1] = (255, 255, 255)
    app._cursor_x, app._cursor_y = 1, 2
    changes = app._handle_char("x")
    assert changes == [{"x": 1, "y": 2, "color": [10, 10, 10]}]


def test_palette_keys_select_color():
    app = make_app()
    app._handle_char("3")  # index 2 -> red
    assert app._color == (255, 0, 0)
    app._handle_char("c")  # cycle to index 3 -> yellow
    assert app._color == PALETTE[3][1]


def test_q_sets_quit_flag():
    app = make_app()
    app._handle_char("q")
    assert app._quit.is_set()


# --------------------------------------------------------------------------- #
# Keyboard: cursor movement
# --------------------------------------------------------------------------- #


def test_arrow_keys_move_cursor_within_bounds():
    app = make_app()
    app._handle_csi("", "C")  # right
    assert (app._cursor_x, app._cursor_y) == (1, 0)
    app._handle_csi("", "B")  # down
    assert (app._cursor_x, app._cursor_y) == (1, 1)
    app._handle_csi("", "D")  # left
    app._handle_csi("", "A")  # up
    assert (app._cursor_x, app._cursor_y) == (0, 0)
    # clamping at the edges
    app._handle_csi("", "D")
    app._handle_csi("", "A")
    assert (app._cursor_x, app._cursor_y) == (0, 0)


# --------------------------------------------------------------------------- #
# Mouse: click and click-drag painting (SGR coordinates are 1-based terminal
# rows; row 1 is the header, so row 2 is the first canvas row)
# --------------------------------------------------------------------------- #


def test_mouse_click_paints_both_halves_of_cell():
    app = make_app()
    app._color = (0, 255, 0)
    changes = app._handle_csi("<0;3;2", "M")  # click at terminal (col 3, row 2)
    assert changes == [
        {"x": 2, "y": 0, "color": [0, 255, 0]},
        {"x": 2, "y": 1, "color": [0, 255, 0]},
    ]
    assert app._pixels[1][2] == (0, 255, 0)


def test_mouse_drag_paints():
    app = make_app()
    app._color = (1, 2, 3)
    changes = app._handle_csi("<32;2;3", "M")  # drag to (col 1, row 3)
    assert changes == [
        {"x": 1, "y": 2, "color": [1, 2, 3]},
        {"x": 1, "y": 3, "color": [1, 2, 3]},
    ]


def test_mouse_release_does_not_paint():
    app = make_app()
    app._color = (1, 2, 3)
    assert app._handle_csi("<0;2;2", "m") == []  # 'm' = release


def test_mouse_click_on_header_is_ignored():
    app = make_app()
    app._color = (1, 2, 3)
    assert app._handle_csi("<0;5;1", "M") == []  # row 1 is the header


def test_mouse_right_button_does_not_paint():
    app = make_app()
    app._color = (1, 2, 3)
    assert app._handle_csi("<2;2;2", "M") == []  # button 2 = right


def test_mouse_click_out_of_bounds_is_ignored():
    app = make_app()
    app._color = (1, 2, 3)
    assert app._handle_csi("<0;99;2", "M") == []
