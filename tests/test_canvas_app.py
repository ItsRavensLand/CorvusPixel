"""Tests for the interactive canvas app's input handling and rendering.

These drive the input handlers directly with synthetic bytes/events — no real
terminal needed. They verify that keyboard and mouse input produce the correct
pixel writes on the app's copy of the canvas, that the square-pixel + centering
math is right, that the cursor always renders at the current pixel, and that
interactive resize preserves/discards data at the edges.

Run with:  .venv/bin/python -m pytest
"""

import io

from rich.console import Console

from canvas_app import (
    BRUSH_MAX,
    BRUSH_MIN,
    CELL_W,
    MAX_CANVAS,
    MIN_CANVAS,
    PALETTE,
    QUICK_COLORS,
    CanvasApp,
    ToolButton,
)

FULL = {
    "type": "full",
    "width": 4,
    "height": 4,
    "background": [10, 10, 10],
    "pixels": [[[10, 10, 10]] * 4 for _ in range(4)],
}

# With a 4x4 canvas in an 80x40 terminal:
#   canvas_rows = 2, content height = header + toolbar + canvas + palette + status
#   = 1 + 1 + 2 + 2 + 1 = 7
#   top_pad = (40-7)//2 = 16, left_pad = (80 - 4*2)//2 = 36
SIZE = (80, 40)
CONTENT_H = 7


def make_app(size=SIZE) -> CanvasApp:
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=size[0], height=size[1], force_interactive=True, legacy_windows=False,
    )
    app = CanvasApp(
        "/tmp/nonexistent.sock", background=(10, 10, 10),
        console=console, input_stream=io.StringIO(), size_provider=lambda: size,
    )
    app._blink_active = True  # deterministic cursor for rendering tests
    app._apply(FULL)  # set the canvas + dimensions, like the first socket message
    return app


def cell_screen(app: CanvasApp, x: int, dy: int) -> tuple[int, int]:
    """Terminal (1-based col, row) of the top-left of a canvas display cell."""
    l = app._layout_info
    return (l["left_pad"] + x * CELL_W + 1, l["top_pad"] + 3 + dy)


def palette_swatch_screen(app: CanvasApp, idx: int) -> tuple[int, int]:
    """Terminal (1-based col, row) of a palette swatch."""
    g = app._palette_geometry()
    return (g["indent"] + idx * (g["swatch_w"] + g["gap"]) + 1, g["row"])


def toolbar_button(app: CanvasApp, ident: str) -> ToolButton:
    """The toolbar ``ToolButton`` with the given ident."""
    for b in app._toolbar_geometry()["buttons"]:
        if b.ident == ident:
            return b
    raise AssertionError(f"no toolbar button {ident!r}")


def click_toolbar(app: CanvasApp, ident: str):
    """Dispatch a mouse click on the toolbar button ``ident``."""
    b = toolbar_button(app, ident)
    return app._handle_csi(f"<0;{b.col};{b.row}", "M")


def slider_slot(app: CanvasApp, size: int) -> tuple[int, int]:
    """Terminal (col, row) of the brush-bar track cell for a brush size."""
    s = app._toolbar_geometry()["slider"]
    assert s is not None
    return (s.col + (size - 1), s.row)


# --------------------------------------------------------------------------- #
# Keyboard: painting (single-pixel brush)
# --------------------------------------------------------------------------- #


def test_space_paints_at_cursor():
    app = make_app()
    app._color = (255, 0, 0)
    changes = app._handle_char(" ")
    assert changes == [{"x": 0, "y": 0, "color": [255, 0, 0]}]
    assert app._pixels[0][0] == (255, 0, 0)


def test_space_paints_idempotently():
    """Painting the same pixel twice keeps the colour — it must not toggle."""
    app = make_app()
    app._color = (255, 0, 0)
    app._handle_char(" ")
    assert app._pixels[0][0] == (255, 0, 0)
    second = app._handle_char(" ")  # repeat paint: no-op, stays red
    assert second == []
    assert app._pixels[0][0] == (255, 0, 0)


def test_eraser_is_the_only_way_to_unpaint():
    """Toggling the eraser tool then painting erases; plain paint never does."""
    app = make_app()
    app._color = (255, 0, 0)
    app._handle_char(" ")
    app._handle_char("e")  # eraser on
    changes = app._handle_char(" ")
    assert changes == [{"x": 0, "y": 0, "color": [10, 10, 10]}]
    assert app._pixels[0][0] == (10, 10, 10)
    app._handle_char("e")  # eraser off again
    assert app._pixels[0][0] == (10, 10, 10)  # still background
    app._handle_char(" ")  # plain paint returns
    assert app._pixels[0][0] == (255, 0, 0)


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
# Mouse: click and click-drag painting (SGR coords are 1-based; the header is
# at row top_pad+1, the first canvas display row at top_pad+2)
# --------------------------------------------------------------------------- #


def test_mouse_click_paints_both_halves_of_cell():
    app = make_app()
    app._color = (0, 255, 0)
    col, row = cell_screen(app, 2, 0)  # pixel (2, 0) and (2, 1)
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    assert changes == [
        {"x": 2, "y": 0, "color": [0, 255, 0]},
        {"x": 2, "y": 1, "color": [0, 255, 0]},
    ]
    assert app._pixels[1][2] == (0, 255, 0)


def test_mouse_drag_paints():
    app = make_app()
    app._color = (1, 2, 3)
    col, row = cell_screen(app, 1, 1)  # drag to pixel (1, 2) and (1, 3)
    changes = app._handle_csi(f"<32;{col};{row}", "M")
    assert changes == [
        {"x": 1, "y": 2, "color": [1, 2, 3]},
        {"x": 1, "y": 3, "color": [1, 2, 3]},
    ]


def test_mouse_release_does_not_paint():
    app = make_app()
    app._color = (1, 2, 3)
    col, row = cell_screen(app, 2, 0)
    assert app._handle_csi(f"<0;{col};{row}", "m") == []  # 'm' = release


def test_mouse_click_on_header_is_ignored():
    app = make_app()
    app._color = (1, 2, 3)
    l = app._layout_info
    assert app._handle_csi(f"<0;5;{l['top_pad'] + 1}", "M") == []  # header row


def test_mouse_right_button_does_not_paint():
    app = make_app()
    app._color = (1, 2, 3)
    col, row = cell_screen(app, 2, 0)
    assert app._handle_csi(f"<2;{col};{row}", "M") == []  # button 2 = right


def test_mouse_click_out_of_bounds_is_ignored():
    app = make_app()
    app._color = (1, 2, 3)
    assert app._handle_csi("<0;999;2", "M") == []


# --------------------------------------------------------------------------- #
# Brush tools: eraser + adjustable square brush size
# --------------------------------------------------------------------------- #


def test_brush_size_applies_square():
    app = make_app()
    app._color = (255, 0, 0)
    app._cursor_x, app._cursor_y = 2, 2
    app._brush_size = 3
    changes = app._handle_char(" ")
    assert len(changes) == 9
    assert {(c["x"], c["y"]) for c in changes} == {
        (x, y) for y in range(1, 4) for x in range(1, 4)
    }
    assert all(c["color"] == [255, 0, 0] for c in changes)
    assert app._pixels[2][2] == (255, 0, 0)


def test_brush_size_clips_at_edges():
    app = make_app()
    app._color = (0, 255, 0)
    app._brush_size = 3
    # cursor at (0,0): only the bottom-right quadrant of the brush fits
    changes = app._handle_char(" ")
    assert {(c["x"], c["y"]) for c in changes} == {(0, 0), (1, 0), (0, 1), (1, 1)}


def test_brush_size_cycle_hotkeys():
    app = make_app()
    app._handle_char("+")
    assert app._brush_size == 2
    app._handle_char("=")
    assert app._brush_size == 3
    app._handle_char("-")
    assert app._brush_size == 2
    app._brush_size = BRUSH_MAX
    app._handle_char("+")  # clamps at the top
    assert app._brush_size == BRUSH_MAX
    app._handle_char("-")  # from the top
    assert app._brush_size == BRUSH_MAX - 1


def test_eraser_paints_background():
    app = make_app()
    app._color = (255, 0, 0)
    app._pixels[2][1] = (255, 255, 255)
    app._cursor_x, app._cursor_y = 1, 2
    app._handle_char("e")
    assert app._eraser is True
    changes = app._handle_char(" ")
    assert changes == [{"x": 1, "y": 2, "color": [10, 10, 10]}]
    app._handle_char("e")
    assert app._eraser is False


def test_eraser_with_mouse():
    app = make_app()
    app._color = (255, 0, 0)
    app._pixels[0][1] = (255, 255, 255)
    app._pixels[1][1] = (255, 255, 255)
    app._handle_char("e")
    col, row = cell_screen(app, 1, 0)
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    assert {(c["x"], c["y"]) for c in changes} == {(1, 0), (1, 1)}
    assert all(c["color"] == [10, 10, 10] for c in changes)


# --------------------------------------------------------------------------- #
# Visual color palette: swatches selected by arrow keys or mouse click
# --------------------------------------------------------------------------- #


def test_palette_swatch_click_selects_color():
    app = make_app()
    col, row = palette_swatch_screen(app, 2)  # red
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    assert changes == []  # a palette click never paints pixels
    assert app._palette_idx == 2
    assert app._color == (255, 0, 0)


def test_mouse_click_on_palette_indicator_row_is_ignored():
    app = make_app()
    g = app._palette_geometry()
    changes = app._handle_csi(f"<0;{g['indent'] + 2};{g['indicator_row']}", "M")
    assert changes == []


def test_palette_mode_arrows_and_confirm():
    app = make_app()
    app._handle_char("\t")
    assert app._palette_mode is True
    app._handle_csi("", "C")  # right -> index 1
    assert app._palette_idx == 1
    app._handle_csi("", "B")  # down -> +4
    assert app._palette_idx == 5
    app._handle_csi("", "A")  # up -> -4
    assert app._palette_idx == 1
    app._handle_char(" ")  # confirm -> exit palette mode
    assert app._palette_mode is False
    assert app._color == PALETTE[1][1]


def test_palette_mode_tab_toggles_off():
    app = make_app()
    app._handle_char("\t")
    assert app._palette_mode is True
    app._handle_char("\t")
    assert app._palette_mode is False


# --------------------------------------------------------------------------- #
# Visible cursor: always rendered (reversed) at the current pixel, tracking
# cleanly with no leftover artifacts at the previous position
# --------------------------------------------------------------------------- #


def test_cursor_renders_at_position_and_tracks():
    app = make_app()
    out = app._console.file
    first = out.getvalue()
    l = app._layout_info
    pos_cell = lambda x: f"\x1b[{l['top_pad'] + 3};{l['left_pad'] + x * CELL_W + 1}H"

    # move to (1,0): old cell (0,0) restored plain, new cell (1,0) reversed
    app._cursor_x, app._cursor_y = 1, 0
    app._draw()
    delta = out.getvalue()[len(first):]
    assert pos_cell(1) in delta  # cursor now at (1,0)
    assert "\x1b[7m" in delta
    idx = delta.find(pos_cell(0))
    assert idx != -1 and "\x1b[7m" not in delta[idx:idx + 40]  # old cell plain

    # move back to (0,0): the (1,0) cell is restored plain, (0,0) reversed
    second = out.getvalue()
    app._cursor_x, app._cursor_y = 0, 0
    app._draw()
    delta2 = out.getvalue()[len(second):]
    assert pos_cell(0) in delta2
    assert "\x1b[7m" in delta2
    idx2 = delta2.find(pos_cell(1))
    assert idx2 != -1 and "\x1b[7m" not in delta2[idx2:idx2 + 40]


def test_cursor_blink_phase_flips_render():
    app = make_app()
    out = app._console.file
    first = out.getvalue()
    app._blink_active = False  # blink phase turns off
    app._draw()
    delta = out.getvalue()[len(first):]
    assert "\x1b[7m" not in delta  # cursor rendered plain while blinking off
    app._blink_active = True
    app._draw()
    assert "\x1b[7m" in out.getvalue()[len(first) + len(delta):]


def test_cursor_blinks_over_time_via_pump():
    """The runtime blink path: _pump() syncs _blink_active from _blink_on() and
    redraws, so the cursor cell alternates reversed/plain every ~500 ms."""
    app = make_app()
    out = app._console.file
    first = out.getvalue()

    app._blink_on = lambda: False
    app._pump()
    assert app._blink_active is False
    off = out.getvalue()[len(first):]
    assert "\x1b[7m" not in off  # cursor restored plain
    assert "\x1b[48;2;0;0;0m]" not in off  # bracket frame hidden in the off phase

    app._blink_on = lambda: True
    app._pump()
    assert app._blink_active is True
    on = out.getvalue()[len(first) + len(off):]
    assert "\x1b[7m" in on  # cursor reversed again
    assert "\x1b[48;2;0;0;0m]" in on  # and framed by its brackets


def test_cursor_brackets_always_visible_on_blank_canvas():
    """On an empty canvas reverse video is invisible (fg == bg), so the cursor
    is framed by bright brackets that make its position unambiguous."""
    app = make_app()
    rendered = app._console.file.getvalue()
    l = app._layout_info
    cursor_row = l["top_pad"] + 3  # cursor at (0,0)
    # right bracket: one column past the cursor cell, drawn bright-on-black
    right = f"\x1b[{cursor_row};{l['left_pad'] + CELL_W + 1}H"
    assert right in rendered
    assert "\x1b[48;2;0;0;0m]\x1b[0m" in rendered


# --------------------------------------------------------------------------- #
# Square pixels: 2 terminal columns per logical pixel (with half-block rows)
# --------------------------------------------------------------------------- #


def test_square_pixel_two_columns():
    app = make_app()
    out = app._console.file
    first = len(out.getvalue())
    app._apply({"type": "update", "changes": [{"x": 0, "y": 0, "color": [255, 0, 0]}]})
    delta = out.getvalue()[first:]
    l = app._layout_info
    assert f"\x1b[{l['top_pad'] + 3};{l['left_pad'] + 1}H" in delta
    assert delta.count("▀") == 2  # exactly one cell = two half-block columns
    assert "▀▀" in delta


# --------------------------------------------------------------------------- #
# Canvas centering at a few terminal sizes
# --------------------------------------------------------------------------- #


def test_canvas_centering_at_terminal_sizes():
    for size in [(80, 24), (120, 40), (60, 20)]:
        app = make_app(size=size)
        out = app._console.file
        l = app._layout_info
        assert l["content_h"] == CONTENT_H  # header + toolbar + canvas + palette + status
        assert l["top_pad"] == max(0, (size[1] - CONTENT_H) // 2)
        assert l["left_pad"] == max(0, (size[0] - 4 * CELL_W) // 2)
        assert f"\x1b[{l['top_pad'] + 1};1H" in out.getvalue()  # header is centered


# --------------------------------------------------------------------------- #
# Interactive resize: preserve where it fits, discard at the edges
# --------------------------------------------------------------------------- #


def test_resize_local_preserves_and_discards():
    app = make_app()
    app._pixels[0][0] = (255, 0, 0)
    app._pixels[1][1] = (0, 255, 0)
    app._request_resize(1, 0)  # grow width 4 -> 5 (new column at the right)
    assert app._width == 5
    assert app._pixels[0][0] == (255, 0, 0)  # preserved
    assert app._pixels[0][4] == (10, 10, 10)  # new column = background
    app._request_resize(0, 1)  # grow height 4 -> 5 (new row at the bottom)
    assert app._height == 5
    assert app._pixels[4][4] == (10, 10, 10)
    app._request_resize(-1, 0)  # shrink width 5 -> 4 (right column dropped)
    assert app._width == 4
    assert app._pixels[0][3] == (10, 10, 10)  # dropped column's data gone
    assert app._pixels[0][0] == (255, 0, 0)  # still fits -> preserved
    app._request_resize(0, -1)  # shrink height 5 -> 4 (bottom row dropped)
    assert app._height == 4
    assert app._pixels[1][1] == (0, 255, 0)


def test_resize_cursor_clamps():
    app = make_app()
    app._cursor_x, app._cursor_y = 3, 3
    app._request_resize(-1, -1)
    assert (app._cursor_x, app._cursor_y) == (2, 2)


def test_resize_clamps_at_bounds():
    app = make_app()
    for _ in range(200):
        app._request_resize(1, 0)
    assert app._width == MAX_CANVAS
    for _ in range(200):
        app._request_resize(-1, 0)
    assert app._width == MIN_CANVAS


def test_resize_pending_is_sent_and_cleared():
    app = make_app()
    app._request_resize(1, 0)
    assert app._pending_resize == (5, 4)
    app._after_edit([])  # no socket in tests -> send is a no-op, flag still clears
    assert app._pending_resize is None


# --------------------------------------------------------------------------- #
# Clickable toolbar: one button per tool, acting like the keyboard shortcut
# --------------------------------------------------------------------------- #


def test_toolbar_paint_button_paints_at_cursor():
    app = make_app()
    app._color = (255, 0, 0)
    changes = click_toolbar(app, "paint")
    assert changes == [{"x": 0, "y": 0, "color": [255, 0, 0]}]
    assert app._pixels[0][0] == (255, 0, 0)


def test_toolbar_paint_button_paints_idempotently():
    app = make_app()
    app._color = (255, 0, 0)
    click_toolbar(app, "paint")
    assert click_toolbar(app, "paint") == []  # repeat paint: no toggle back
    assert app._pixels[0][0] == (255, 0, 0)


def test_toolbar_eraser_button_toggles():
    app = make_app()
    assert app._eraser is False
    click_toolbar(app, "eraser")
    assert app._eraser is True
    click_toolbar(app, "eraser")
    assert app._eraser is False


def test_toolbar_brush_buttons_step_and_clamp():
    app = make_app()
    click_toolbar(app, "brush_inc")
    assert app._brush_size == 2
    click_toolbar(app, "brush_dec")
    assert app._brush_size == 1
    click_toolbar(app, "brush_dec")  # clamps at the minimum
    assert app._brush_size == BRUSH_MIN
    app._brush_size = BRUSH_MAX
    click_toolbar(app, "brush_inc")  # clamps at the maximum
    assert app._brush_size == BRUSH_MAX


def test_toolbar_palette_button_opens_palette_mode():
    app = make_app()
    click_toolbar(app, "palette")
    assert app._palette_mode is True


def test_toolbar_resize_buttons():
    app = make_app()
    click_toolbar(app, "col_inc")
    assert app._width == 5
    assert app._pending_resize == (5, 4)
    click_toolbar(app, "row_inc")
    assert app._height == 5
    assert app._pending_resize == (5, 5)
    click_toolbar(app, "col_dec")
    assert app._width == 4
    click_toolbar(app, "row_dec")
    assert app._height == 4


def test_toolbar_quit_button_sets_quit():
    app = make_app()
    click_toolbar(app, "quit")
    assert app._quit.is_set()


def test_toolbar_spacing_click_is_ignored():
    app = make_app()
    app._color = (1, 2, 3)
    g = app._toolbar_geometry()
    assert app._handle_csi(f"<0;1;{g['row']}", "M") == []  # padding before buttons
    b = toolbar_button(app, "paint")
    assert app._handle_csi(f"<0;{b.col + b.width};{g['row']}", "M") == []  # gap
    assert app._pixels[0][0] == (10, 10, 10)  # nothing painted


def test_toolbar_render_eraser_active_state():
    app = make_app()
    assert "[Eraser]" in app._render_toolbar()
    assert "\x1b[7m[Eraser]" not in app._render_toolbar()  # off = plain
    app._eraser = True
    assert "\x1b[7m[Eraser]" in app._render_toolbar()  # on = reversed


# --------------------------------------------------------------------------- #
# Brush-size slider (Paint-style): click, live drag, clamping, throttling
# --------------------------------------------------------------------------- #


def test_slider_click_sets_brush_size():
    app = make_app()
    col, row = slider_slot(app, 5)
    assert app._handle_csi(f"<0;{col};{row}", "M") == []  # never paints pixels
    assert app._brush_size == 5


def test_slider_click_at_ends_clamps():
    app = make_app()
    col, row = slider_slot(app, 1)
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._brush_size == 1
    col, row = slider_slot(app, BRUSH_MAX)
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._brush_size == BRUSH_MAX
    # one column past the right end of the track still clamps to the maximum
    s = app._toolbar_geometry()["slider"]
    app._handle_csi(f"<0;{s.col + s.width};{s.row}", "M")
    assert app._brush_size == BRUSH_MAX


def test_slider_drag_updates_live():
    app = make_app()
    s = app._toolbar_geometry()["slider"]
    app._handle_csi(f"<0;{s.col};{s.row}", "M")  # press at size 1
    assert app._brush_size == 1
    app._handle_csi(f"<32;{s.col + 3};{s.row}", "M")  # drag to size 4
    assert app._brush_size == 4
    app._handle_csi(f"<32;{s.col + 6};{s.row}", "M")  # drag to size 7
    assert app._brush_size == BRUSH_MAX


def test_slider_drag_past_ends_clamps():
    app = make_app()
    s = app._toolbar_geometry()["slider"]
    app._handle_csi(f"<0;{s.col - 1};{s.row}", "M")  # left margin -> size 1
    assert app._brush_size == 1
    app._handle_csi(f"<32;{s.col + s.width};{s.row}", "M")  # right margin -> max
    assert app._brush_size == BRUSH_MAX
    # dragging well beyond the track is ignored and keeps the clamp
    app._handle_csi(f"<32;{s.col + 20};{s.row}", "M")
    assert app._brush_size == BRUSH_MAX
    assert app._pixels[0][0] == (10, 10, 10)  # never a canvas paint


def test_slider_handle_renders_at_brush_size():
    app = make_app()
    app._brush_size = 3
    rendered = app._render_toolbar()
    s = app._toolbar_geometry()["slider"]
    assert f"\x1b[{s.row};{s.col}H" in rendered  # track starts at the slider
    assert "\x1b[7m●" in rendered  # the handle
    assert rendered.count("─") == BRUSH_MAX - 1  # rest of the track


def test_slider_drag_redraws_toolbar_live():
    app = make_app()
    out = app._console.file
    s = app._toolbar_geometry()["slider"]
    first = len(out.getvalue())
    app._handle_csi(f"<0;{s.col + 4};{s.row}", "M")  # size 5
    app._after_edit([])
    delta = out.getvalue()[first:]
    l = app._layout_info
    assert app._brush_size == 5
    assert f"\x1b[{l['top_pad'] + 2};1H\x1b[2K" in delta  # toolbar redrawn
    assert "\x1b[7m●" in delta  # handle drawn at the new size
    assert f"\x1b[{l['top_pad'] + 3};" not in delta  # canvas cells untouched


def test_slider_drag_same_slot_is_noop():
    app = make_app()
    out = app._console.file
    s = app._toolbar_geometry()["slider"]
    app._handle_csi(f"<0;{s.col + 2};{s.row}", "M")  # size 3: a real change
    app._after_edit([])  # chrome-only redraw
    first = len(out.getvalue())
    app._handle_csi(f"<32;{s.col + 2};{s.row}", "M")  # same slot: nothing changed
    app._after_edit([])
    delta = out.getvalue()[first:]
    # Only the terminal-cursor park move is emitted — no canvas or chrome writes.
    assert "\x1b[38;2;" not in delta
    assert "▀" not in delta
    assert "●" not in delta
    assert delta.count("\x1b[") == 1


# --------------------------------------------------------------------------- #
# Toolbar layout + palette interplay: centering survives the extra row
# --------------------------------------------------------------------------- #


def test_toolbar_layout_keeps_canvas_centered():
    for size in [(80, 24), (120, 40), (60, 20)]:
        app = make_app(size=size)
        out = app._console.file
        l = app._layout_info
        g = app._toolbar_geometry(l)
        assert g["row"] == l["top_pad"] + 2  # toolbar just below the header
        assert l["content_h"] == CONTENT_H
        assert l["top_pad"] == max(0, (size[1] - CONTENT_H) // 2)
        assert l["left_pad"] == max(0, (size[0] - 4 * CELL_W) // 2)
        rendered = out.getvalue()
        assert f"\x1b[{g['row']};1H\x1b[2K" in rendered  # toolbar row drawn
        assert f"\x1b[{l['top_pad'] + 3};{l['left_pad'] + 1}H" in rendered


def test_swatch_click_while_in_palette_mode_confirms():
    app = make_app()
    app._handle_char("\t")
    assert app._palette_mode is True
    col, row = palette_swatch_screen(app, 4)
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._palette_mode is False  # a swatch click acts like Enter
    assert app._color == PALETTE[4][1]


# --------------------------------------------------------------------------- #
# Toolbar centering + Unicode icons
# --------------------------------------------------------------------------- #


def test_toolbar_is_horizontally_centered():
    for size in [(80, 24), (120, 40)]:  # wide enough for the text-mode toolbar
        app = make_app(size=size)
        g = app._toolbar_geometry(app._layout_info)
        buttons = g["buttons"]
        assert buttons
        first = buttons[0].col
        last = buttons[-1].col + buttons[-1].width - 1
        # padding on the left of the toolbar matches the padding on the right
        assert abs((first - 1) - (size[0] - last)) <= 1
        assert g["indent"] == max(0, (size[0] - 69) // 2)  # text mode: 69 cols


def test_toolbar_too_narrow_clamps_to_left():
    app = make_app(size=(40, 24))
    g = app._toolbar_geometry(app._layout_info)
    assert g["indent"] == 0  # doesn't overflow the terminal, clamps to the left


def test_toolbar_icons_render_when_enabled():
    app = make_app()
    assert app._icons is False  # StringIO console has no encoding -> text fallback
    app._icons = True
    rendered = app._render_toolbar()
    assert "[Paint]" not in rendered
    assert "●" in rendered  # paint icon
    b = toolbar_button(app, "paint")
    assert b.width == 1  # icons are single-width, so geometry shrinks
    app._eraser = True
    assert "\x1b[7m▨" in app._render_toolbar()  # active tool still reverses


def test_toolbar_icons_fit_narrow_terminals_and_stay_centered():
    app = make_app(size=(40, 24))
    app._icons = True
    g = app._toolbar_geometry(app._layout_info)
    buttons = g["buttons"]
    first = buttons[0].col
    last = buttons[-1].col + buttons[-1].width - 1
    assert abs((first - 1) - (40 - last)) <= 1  # centered even at 40 cols


def test_icons_override_env_forces_icons():
    import os

    app = make_app()
    try:
        os.environ["CORVUSPIXEL_ICONS"] = "1"
        assert app._use_icons() is True
        os.environ["CORVUSPIXEL_ICONS"] = "0"
        assert app._use_icons() is False
    finally:
        del os.environ["CORVUSPIXEL_ICONS"]


# --------------------------------------------------------------------------- #
# Bottom colour row: quick colours + custom-colour hex input
# --------------------------------------------------------------------------- #


def quick_swatch_screen(app: CanvasApp, idx: int) -> tuple[int, int]:
    """Terminal (1-based col, row) of a quick-colour swatch (0 = orange)."""
    g = app._quick_geometry()
    return (g["indent"] + idx * (g["swatch_w"] + g["gap"]) + 1, g["row"])


def custom_swatch_screen(app: CanvasApp) -> tuple[int, int]:
    """Terminal (1-based col, row) of the rainbow custom-colour swatch."""
    g = app._quick_geometry()
    idx = len(QUICK_COLORS)
    return (g["indent"] + idx * (g["swatch_w"] + g["gap"]) + 1, g["row"])


def test_quick_colors_row_replaces_the_legend():
    app = make_app()
    rendered = app._console.file.getvalue()
    assert "mouse:" not in rendered  # the old keyboard-shortcut legend is gone
    g = app._quick_geometry()
    assert f"\x1b[{g['row']};1H\x1b[2K" in rendered  # quick row drawn
    # a quick swatch is a solid 4-column half-block block
    assert f"\x1b[{g['row']};{g['indent'] + 1}H" in rendered


def test_quick_swatch_click_selects_color():
    app = make_app()
    col, row = quick_swatch_screen(app, 0)  # orange
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    assert changes == []  # a colour pick never paints pixels
    assert app._color == QUICK_COLORS[0][1]
    assert app._quick_idx == 0
    assert app._palette_active is False  # the palette highlight gives way


def test_quick_swatch_click_while_in_palette_mode_confirms():
    app = make_app()
    app._handle_char("\t")
    col, row = quick_swatch_screen(app, 2)
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._palette_mode is False
    assert app._color == QUICK_COLORS[2][1]


def test_quick_swatch_is_reversed_when_selected():
    app = make_app()
    out = app._console.file
    first = len(out.getvalue())
    app._select_quick_color(3)
    app._after_edit([])  # chrome-only redraw writes the quick row
    delta = out.getvalue()[first:]
    g = app._quick_geometry()
    idx = g["indent"] + 3 * (g["swatch_w"] + g["gap"]) + 1
    assert f"\x1b[{g['row']};{idx}H" in delta  # selected swatch position
    assert "\x1b[7m" in delta  # ... drawn reversed


def test_palette_swatch_click_clears_quick_highlight():
    app = make_app()
    app._select_quick_color(1)
    col, row = palette_swatch_screen(app, 2)
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._quick_idx is None
    assert app._palette_active is True
    assert app._color == PALETTE[2][1]


def test_custom_color_swatch_opens_hex_input_and_confirms():
    app = make_app()
    col, row = custom_swatch_screen(app)
    assert app._handle_csi(f"<0;{col};{row}", "M") == []
    assert app._custom_color_mode is True
    for ch in "ffA0b3":
        app._handle_char(ch)
    assert app._hex_buffer == "ffa0b3"
    app._handle_char("\r")
    assert app._custom_color_mode is False
    assert app._color == (0xFF, 0xA0, 0xB3)


def test_custom_color_hex_input_limits_and_backspace():
    app = make_app()
    app._open_custom_color()
    for ch in "abcdef12":  # 8 chars, only 6 are kept
        app._handle_char(ch)
    assert app._hex_buffer == "abcdef"
    app._handle_char("\x7f")  # backspace
    assert app._hex_buffer == "abcde"
    app._handle_char("\x7f")
    assert app._hex_buffer == "abcd"


def test_custom_color_hex_input_enter_requires_six_digits():
    app = make_app()
    app._open_custom_color()
    app._handle_char("ff")
    app._handle_char("\r")  # only 2 digits: ignored, still in hex mode
    assert app._custom_color_mode is True
    assert app._color == PALETTE[0][1]


def test_custom_color_hex_input_cancels_on_escape():
    app = make_app()
    app._open_custom_color()
    app._handle_char("1")
    app._read_escape_sequence()  # a stray Esc arrives (input stream is empty)
    assert app._custom_color_mode is False
    assert app._hex_buffer == ""


def test_mouse_click_cancels_custom_color_input():
    app = make_app()
    app._open_custom_color()
    app._handle_char("ff")
    col, row = cell_screen(app, 2, 0)
    app._handle_csi(f"<0;{col};{row}", "M")  # click on the canvas
    assert app._custom_color_mode is False
    assert app._hex_buffer == ""


def test_click_outside_bottom_row_is_ignored():
    app = make_app()
    g = app._quick_geometry()
    # one row below the quick-colour row: nothing there to click
    assert app._handle_csi(f"<0;{g['indent'] + 2};{g['row'] + 1}", "M") == []
    assert app._pixels[0][0] == (10, 10, 10)
