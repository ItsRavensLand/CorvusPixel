"""Tests for the interactive canvas app's input handling and rendering.

These drive the input handlers directly with synthetic bytes/events — no real
terminal needed. They verify that keyboard and mouse input produce the correct
pixel writes on the app's copy of the canvas, that the square-pixel + centering
math is right, that the cursor always renders at the current pixel, and that
interactive resize preserves/discards data at the edges.

Run with:  .venv/bin/python -m pytest
"""

import io
import re
import socket
import string

from rich.console import Console

from canvas_app import (
    BRUSH_MAX,
    BRUSH_MIN,
    CELL_W,
    FONT_H,
    FONT_W,
    MAX_CANVAS,
    MIN_CANVAS,
    PALETTE,
    QUICK_COLORS,
    TOOL_ERASER,
    TOOL_FILL,
    TOOL_FILLED_RECT,
    TOOL_FILLED_SQUARE,
    TOOL_HOLLOW_RECT,
    TOOL_HOLLOW_SQUARE,
    TOOL_LINE,
    TOOL_PAINT,
    TOOL_TEXT,
    _FONT5X7,
    CanvasApp,
    ToolButton,
    bresenham,
    fill_rect_cells,
    flood_fill_region,
    glyph_pixels,
    hollow_rect_cells,
    square_end,
    thick_line_cells,
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
    assert app._tool == TOOL_ERASER
    changes = app._handle_char(" ")
    assert changes == [{"x": 1, "y": 2, "color": [10, 10, 10]}]
    app._handle_char("e")
    assert app._tool == TOOL_PAINT


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
    assert app._tool == TOOL_PAINT
    click_toolbar(app, "eraser")
    assert app._tool == TOOL_ERASER
    click_toolbar(app, "eraser")
    assert app._tool == TOOL_PAINT


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
    app._tool = TOOL_ERASER
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
    app._tool = TOOL_ERASER
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


# --------------------------------------------------------------------------- #
# Regression tests: over-large canvases and the socket read loop
# --------------------------------------------------------------------------- #
#
# These pin the fixes for the last UI regression round:
#   * a canvas bigger than the window is clamped + clipped, never wrapped or
#     scrolled (the "split background", "invisible toolbar" and horizontal
#     scrolling regressions);
#   * swatch clicks always select, never paint, even when the layout overflows
#     (the "swatch click paints the swatch" regression);
#   * the socket read loop survives idle timeouts and split messages (the
#     connection-flicker regression).


BIG = {
    "type": "full",
    "width": 100,
    "height": 100,
    "background": [10, 10, 10],
    "pixels": [[[10, 10, 10]] * 100 for _ in range(100)],
}


def make_big_app(size=SIZE) -> CanvasApp:
    """A 100x100 canvas in a small terminal — the overflow scenario."""
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=size[0], height=size[1], force_interactive=True, legacy_windows=False,
    )
    app = CanvasApp(
        "/tmp/nonexistent.sock", background=(10, 10, 10),
        console=console, input_stream=io.StringIO(), size_provider=lambda: size,
    )
    app._blink_active = True
    app._apply(BIG)
    return app


def test_layout_clamps_big_canvas_so_chrome_fits():
    for size in [(60, 20), (169, 45), (40, 12)]:
        app = make_big_app(size=size)
        l = app._compute_layout()
        assert l["canvas_rows"] == min(50, size[1] - 5)
        assert l["content_h"] == 5 + l["canvas_rows"]
        assert l["content_h"] <= size[1]  # chrome included, everything fits
        assert l["top_pad"] + 5 + l["canvas_rows"] <= size[1]  # last chrome row
        assert l["left_pad"] == max(0, (size[0] - 100 * CELL_W) // 2)


def test_display_rows_shows_only_visible_top_rows():
    app = make_big_app(size=(60, 20))
    rows = app._display_rows()
    assert len(rows) == app._compute_layout()["canvas_rows"] == 15
    assert all(len(row) == 100 for row in rows)  # width never truncates


def test_write_cell_clips_off_screen_cells():
    app = make_big_app(size=(40, 10))
    l = app._compute_layout()
    assert l["left_pad"] == 0  # the 100-col canvas overflows the 40-col window
    file = app._console.file
    before = len(file.getvalue())
    app._write_cell(file, 0, 30, ((255, 0, 0), (0, 0, 0)))  # right edge > term_w
    assert len(file.getvalue()) == before  # clipped: nothing written
    app._write_cell(file, 0, 0, ((255, 0, 0), (0, 0, 0)))  # fits
    assert len(file.getvalue()) > before


def test_screen_cell_rejects_clipped_columns_and_rows():
    app = make_big_app(size=(40, 10))
    l = app._compute_layout()
    row0 = l["top_pad"] + 3
    assert app._screen_cell(1 + l["left_pad"] + 19 * CELL_W, row0) == (19, 0)
    assert app._screen_cell(1 + l["left_pad"] + 20 * CELL_W, row0) is None
    # rows below the visible canvas are never canvas cells
    assert app._screen_cell(1 + l["left_pad"] + 1, row0 + l["canvas_rows"]) is None


def test_big_canvas_redraw_never_writes_past_term_width():
    app = make_big_app(size=(60, 20))
    out = app._console.file.getvalue()
    max_col = 0
    for m in re.finditer(r"\x1b\[(\d+);(\d+)H", out):
        max_col = max(max_col, int(m.group(2)))
    assert max_col <= 60  # no wrap: every cursor move stays on the screen


def test_overflowed_palette_swatch_click_selects_never_paints():
    app = make_big_app(size=(60, 20))
    for idx in range(len(PALETTE)):
        col, row = palette_swatch_screen(app, idx)
        app._handle_csi(f"<0;{col};{row}", "M")
        assert app._color == PALETTE[idx][1]
        assert app._palette_idx == idx
    for y in range(app._height):
        assert all(p == (10, 10, 10) for p in app._pixels[y])  # never painted


def test_overflowed_quick_swatch_click_selects_never_paints():
    app = make_big_app(size=(60, 20))
    for idx in range(len(QUICK_COLORS)):
        col, row = quick_swatch_screen(app, idx)
        app._handle_csi(f"<0;{col};{row}", "M")
        assert app._color == QUICK_COLORS[idx][1]
        assert app._quick_idx == idx
    for y in range(app._height):
        assert all(p == (10, 10, 10) for p in app._pixels[y])


def test_overflowed_canvas_click_still_paints():
    app = make_big_app(size=(60, 20))
    col, row = cell_screen(app, 0, 0)  # top-left cell: always on-screen
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._pixels[0][0] == PALETTE[0][1]  # painted, not swallowed


def test_overflowed_swatch_row_gap_click_is_ignored():
    app = make_big_app(size=(60, 20))
    g = app._quick_geometry()
    # the padding column left of the first swatch on the quick-colour row
    app._handle_csi(f"<0;{max(1, g['indent'])};{g['row']}", "M")
    assert all(p == (10, 10, 10) for row in app._pixels for p in row)


class FakeSocket:
    """A minimal socket stub that replays queued recv() events."""

    def __init__(self, events):
        self._events = list(events)
        self.timeout = 0.2

    def recv(self, _n):
        if not self._events:
            return b""
        kind = self._events.pop(0)
        if kind == ("timeout",):
            raise socket.timeout("timed out")
        if kind == ("eof",):
            return b""
        return kind[1]

    def close(self):
        pass


class PumpSpy(CanvasApp):
    """A CanvasApp that counts socket traffic without drawing anything."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.applied = []
        self.pumps = 0

    def _draw(self):
        pass

    def _pump(self):
        self.pumps += 1

    def _apply(self, message):
        self.applied.append(message)
        super()._apply(message)


FULL2 = (
    b'{"type":"full","width":2,"height":2,"background":[0,0,0],'
    b'"pixels":[[[0,0,0],[0,0,0]],[[0,0,0],[0,0,0]]]}\n'
)


def test_pump_socket_survives_timeouts_and_split_messages():
    app = PumpSpy("/tmp/nonexistent.sock", size_provider=lambda: (80, 40))
    update = b'{"type":"update","changes":[{"x":1,"y":0,"color":[255,0,0]}]}\n'
    sock = FakeSocket([
        ("data", FULL2),
        ("timeout",),          # idle stretches must not kill the connection
        ("timeout",),
        ("data", update[:22]),  # a message split across two recv calls...
        ("timeout",),           # ...with an idle timeout in the middle of it
        ("data", update[22:]),
        ("eof",),
    ])
    app._pump_socket(sock)
    assert [m["type"] for m in app.applied] == ["full", "update"]
    assert app._pixels[0][1] == (255, 0, 0)
    assert app.pumps >= 3  # one per idle timeout + one after the update


def test_pump_socket_ignores_garbage_lines():
    app = PumpSpy("/tmp/nonexistent.sock", size_provider=lambda: (80, 40))
    sock = FakeSocket([("data", b"not json\n"), ("eof",)])
    app._pump_socket(sock)
    assert app.applied == []


def test_pump_socket_returns_when_quit_is_set():
    app = PumpSpy("/tmp/nonexistent.sock", size_provider=lambda: (80, 40))
    app._quit.set()
    app._pump_socket(FakeSocket([("timeout",), ("timeout",)]))
    assert app.applied == []



# --------------------------------------------------------------------------- #
# Shape tools: geometry, drag->preview->commit, cancel, thickness
# --------------------------------------------------------------------------- #


def test_bresenham_line_points():
    assert bresenham(0, 0, 0, 3) == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert bresenham(0, 0, 3, 0) == [(0, 0), (1, 0), (2, 0), (3, 0)]
    diag = bresenham(0, 0, 3, 3)
    assert len(diag) == 4 and set(diag) == {(i, i) for i in range(4)}
    assert bresenham(3, 3, 0, 0) == [(3, 3), (2, 2), (1, 1), (0, 0)]  # reversed


def test_fill_rect_cells():
    assert fill_rect_cells(1, 1, 3, 3) == {(x, y) for x in (1, 2, 3) for y in (1, 2, 3)}
    assert fill_rect_cells(3, 3, 1, 1) == fill_rect_cells(1, 1, 3, 3)  # order-free


def test_hollow_rect_border_ring():
    cells = hollow_rect_cells(0, 0, 2, 2, 1)
    assert len(cells) == 8  # a 3x3 border ring
    assert (1, 1) not in cells  # the centre stays hollow
    assert hollow_rect_cells(0, 0, 2, 2, 3) == {(x, y) for x in range(3) for y in range(3)}


def test_hollow_rect_thickness_2():
    cells = hollow_rect_cells(0, 0, 4, 4, 2)
    assert (2, 2) not in cells  # only the 1x1 centre is left hollow
    assert len(cells) == 24  # 25 - 1


def test_thick_line_stamps_brush():
    assert set(thick_line_cells(0, 0, 4, 0, 1)) == {(x, 0) for x in range(5)}
    thick = thick_line_cells(0, 0, 4, 0, 3)
    assert all((x, y) in thick for x in range(5) for y in (-1, 0, 1))


def test_square_end_snaps():
    assert square_end((0, 0), (3, 1)) == (3, 3)
    assert square_end((0, 0), (1, 3)) == (3, 3)
    assert square_end((5, 5), (5, 5)) == (5, 5)
    assert square_end((4, 4), (1, 2)) == (1, 1)  # negative drag direction


def shape_press(app, x, dy):
    col, row = cell_screen(app, x, dy)
    return app._handle_csi(f"<0;{col};{row}", "M")


def shape_drag(app, x, dy):
    col, row = cell_screen(app, x, dy)
    return app._handle_csi(f"<32;{col};{row}", "M")


def shape_release(app, x, dy):
    col, row = cell_screen(app, x, dy)
    return app._handle_csi(f"<3;{col};{row}", "m")


def test_filled_rect_drag_preview_then_commit():
    app = make_app()
    app._color = (255, 0, 0)
    app._tool = TOOL_FILLED_RECT
    # press at display cell (1,0) -> canvas (1,0): nothing committed, preview on
    assert shape_press(app, 1, 0) == []
    assert app._preview_pixels  # preview is showing
    assert app._pixels[0][1] == (10, 10, 10)  # canvas untouched during the drag
    # drag to display cell (2,1) -> canvas (2,2): preview grows, still untouched
    assert shape_drag(app, 2, 1) == []
    assert app._shape_end == (2, 2)
    assert app._pixels[2][2] == (10, 10, 10)
    # release commits the 2x2 box at (1..2, 0..2) as a normal edit
    changes = shape_release(app, 2, 1)
    assert app._preview_pixels == {}
    assert len(changes) == 6
    assert app._pixels[0][1] == (255, 0, 0)
    assert app._pixels[2][2] == (255, 0, 0)


def test_shape_escape_cancels_preview():
    app = make_app()
    app._color = (255, 0, 0)
    app._tool = TOOL_FILLED_RECT
    shape_press(app, 1, 0)
    shape_drag(app, 2, 1)
    assert app._preview_pixels
    # a bare ESC (not a CSI) cancels the drag
    app._input_stream.write("\x1bz")
    app._input_stream.seek(0)
    assert app._read_byte() == b"\x1b"
    assert app._read_escape_sequence() == []
    assert app._preview_pixels == {}
    assert app._shape_drag is None
    assert app._pixels[0][1] == (10, 10, 10)  # the canvas was never touched


def test_hollow_rect_commit_leaves_interior_empty():
    app = make_app()
    app._color = (0, 255, 0)
    app._tool = TOOL_HOLLOW_RECT
    app._brush_size = 1
    # draw from (0,0) to (3,2): a 4x3 box; the ring is 10 cells, centre is hollow
    shape_press(app, 0, 0)
    shape_drag(app, 3, 1)
    changes = shape_release(app, 3, 1)
    assert len(changes) == 10
    assert app._pixels[0][0] == (0, 255, 0)  # border
    assert app._pixels[1][1] == (10, 10, 10)  # interior untouched
    assert app._pixels[1][2] == (10, 10, 10)


def test_filled_square_snaps_to_square():
    app = make_app()
    app._color = (255, 0, 0)
    app._tool = TOOL_FILLED_SQUARE
    # drag from (0,0) across 3 columns and 2 rows -> snapped to a 4x4 square
    shape_press(app, 0, 0)
    shape_drag(app, 3, 1)
    assert app._shape_end == (3, 2)  # raw end point
    changes = shape_release(app, 3, 1)
    assert len(changes) == 16  # 4x4 square, not 4x3


def test_shape_toolbar_right_aligned_and_selects():
    app = make_app()
    shape = app._shape_toolbar_geometry()
    assert shape["row"] == app._layout_info["top_pad"] + 1  # the header row
    assert [b.ident for b in shape["buttons"]] == [
        "filled_rect", "filled_square", "hollow_rect", "hollow_square", "line",
        "fill", "text",
    ]
    last = shape["buttons"][-1]
    assert last.col + last.width - 1 <= 80  # right-aligned, fits the terminal
    b = shape["buttons"][0]
    assert app._handle_csi(f"<0;{b.col};{b.row}", "M") == []
    assert app._tool == TOOL_FILLED_RECT


def test_shape_keyboard_shortcuts_select_tools():
    app = make_app()
    assert app._handle_char("r") == [] and app._tool == TOOL_FILLED_RECT
    assert app._handle_char("o") == [] and app._tool == TOOL_HOLLOW_RECT
    assert app._handle_char("f") == [] and app._tool == TOOL_FILLED_SQUARE
    assert app._handle_char("s") == [] and app._tool == TOOL_HOLLOW_SQUARE
    assert app._handle_char("l") == [] and app._tool == TOOL_LINE
    assert app._handle_char("p") == [] and app._tool == TOOL_PAINT


def test_paint_and_eraser_unaffected_by_shape_tools():
    app = make_app()
    app._color = (255, 0, 0)
    col, row = cell_screen(app, 2, 0)
    assert app._handle_csi(f"<0;{col};{row}", "M") == [
        {"x": 2, "y": 0, "color": [255, 0, 0]},
        {"x": 2, "y": 1, "color": [255, 0, 0]},
    ]  # plain paint unchanged
    app._handle_char("r")  # pick a shape tool...
    app._handle_char("p")  # ...and come back to paint
    assert app._handle_csi(f"<0;{col};{row}", "M") == []  # already red: no-op, no toggle
    app._handle_char("e")  # eraser still toggles
    assert app._tool == TOOL_ERASER


def test_top_line_has_header_and_shape_buttons():
    app = make_app()
    top = app._render_top_line()
    assert "CorvusPixel" in top
    assert "[FR]" in top and "[Line]" in top  # shape buttons in text mode
    app._icons = True
    top = app._render_top_line()
    assert "■" in top and "╱" in top  # shape icons when the terminal supports them


# --------------------------------------------------------------------------- #
# Bottom-left keyboard-shortcut panel
# --------------------------------------------------------------------------- #


def wide_app() -> CanvasApp:
    """A make_app on a 170-col terminal so every shortcut group fits."""
    return make_app((170, 40))


def test_shortcuts_panel_full_grouping_on_wide_terminal():
    app = wide_app()
    # The quick-colour row leaves a 57-column left margin: all six groups fit,
    # packed into lines with a short labelled header per group.
    assert app._shortcuts_panel(avail=57) == [
        [("Move", "←↑↓→"), ("Draw", "space·x·e"), ("Brush", "+·−")],
        [("Shapes", "p·r·o·f·s·l·b·t"), ("Canvas", "[ ]·{ }·Tab"),
         ("Other", "c·1-8·q")],
    ]


def test_shortcuts_panel_packs_by_width_keeping_group_order():
    app = make_app()  # 80 cols -> 12-column left margin
    # Groups stay in priority order and overflow is dropped, never wrapped.
    assert app._shortcuts_panel(avail=12) == [
        [("Move", "←↑↓→")],
        [("Brush", "+·−")],
    ]


def test_shortcuts_panel_hidden_when_margin_too_narrow():
    app = make_app()
    assert app._shortcuts_panel(avail=8) == []


def test_shortcuts_panel_renders_bottom_left():
    app = wide_app()
    rendered = app._console.file.getvalue()
    for header in ("Move", "Draw", "Brush", "Shapes", "Canvas", "Other"):
        assert header in rendered
    assert "space·x·e" in rendered
    assert "p·r·o·f·s·l·b·t" in rendered
    assert "c·1-8·q" in rendered
    # Left-aligned on the palette-indicator row: the bottom-left corner.
    layout = app._layout_info
    panel_row = layout["top_pad"] + 3 + layout["canvas_rows"]
    assert f"\x1b[{panel_row};1H" in rendered


def test_shortcuts_panel_survives_a_colour_pick():
    app = wide_app()
    first = len(app._console.file.getvalue())
    app._handle_char("c")  # cycle colour: the palette row redraws, wiping the panel
    app._after_edit([])
    delta = app._console.file.getvalue()[first:]
    assert "space·x·e" in delta  # the panel is redrawn in the same pass


def test_shortcuts_panel_not_rewritten_on_noop_redraw():
    app = wide_app()
    out = app._console.file
    s = app._toolbar_geometry()["slider"]
    app._handle_csi(f"<0;{s.col + 2};{s.row}", "M")  # size 3: a real chrome change
    app._after_edit([])
    first = len(out.getvalue())
    app._handle_csi(f"<32;{s.col + 2};{s.row}", "M")  # same slot: nothing changed
    app._after_edit([])
    delta = out.getvalue()[first:]
    assert "space·x·e" not in delta  # panel is diffed: not rewritten
    assert "\x1b[38;2;" not in delta  # no header recolour either


# --------------------------------------------------------------------------- #
# Bucket fill: pure flood-fill region + the app tool (single batched update)
# --------------------------------------------------------------------------- #


def test_flood_fill_region_single_isolated_pixel():
    grid = [
        [(10, 10, 10), (10, 10, 10), (10, 10, 10)],
        [(10, 10, 10), (255, 0, 0), (10, 10, 10)],
        [(10, 10, 10), (10, 10, 10), (10, 10, 10)],
    ]
    assert flood_fill_region(grid, 1, 1) == {(1, 1)}


def test_flood_fill_region_fills_whole_canvas():
    grid = [[(0, 0, 0)] * 3 for _ in range(3)]
    assert flood_fill_region(grid, 0, 0) == {(x, y) for x in range(3) for y in range(3)}


def test_flood_fill_region_stops_at_a_wall():
    grid = [
        [(0, 0, 0)] * 5,
        [(0, 0, 0), (255, 0, 0), (255, 0, 0), (255, 0, 0), (0, 0, 0)],
        [(0, 0, 0), (255, 0, 0), (0, 0, 0), (255, 0, 0), (0, 0, 0)],
        [(0, 0, 0), (255, 0, 0), (255, 0, 0), (255, 0, 0), (0, 0, 0)],
        [(0, 0, 0)] * 5,
    ]
    # the enclosed background centre is a separate region from the outside
    assert flood_fill_region(grid, 2, 2) == {(2, 2)}
    outer = flood_fill_region(grid, 0, 0)
    assert (0, 0) in outer and (4, 4) in outer
    assert (2, 2) not in outer  # the red ring is a wall for the fill


def test_flood_fill_region_is_four_connected_not_diagonal():
    grid = [
        [(0, 0, 0), (1, 1, 1)],
        [(1, 1, 1), (0, 0, 0)],
    ]
    assert flood_fill_region(grid, 0, 0) == {(0, 0)}  # only diagonally adjacent


def test_flood_fill_region_out_of_bounds_is_empty():
    assert flood_fill_region([[ (0, 0, 0) ]], -1, 0) == set()
    assert flood_fill_region([], 0, 0) == set()


def test_fill_tool_isolated_pixel_only():
    app = make_app()
    app._tool = TOOL_FILL
    app._pixels[0][2] = (255, 0, 0)  # a lone red pixel, neighbours are background
    app._color = (0, 255, 0)
    col, row = cell_screen(app, 2, 0)  # the cell containing pixel (2,0)
    assert app._handle_csi(f"<0;{col};{row}", "M") == [
        {"x": 2, "y": 0, "color": [0, 255, 0]}
    ]


def test_fill_tool_fills_whole_canvas():
    app = make_app()  # 4x4 all background: one giant region
    app._tool = TOOL_FILL
    app._color = (255, 0, 0)
    col, row = cell_screen(app, 0, 0)
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    assert len(changes) == 16
    assert all(p == (255, 0, 0) for row in app._pixels for p in row)


def test_fill_tool_noop_when_color_matches():
    app = make_app()
    app._tool = TOOL_FILL
    app._color = (10, 10, 10)  # the background colour
    col, row = cell_screen(app, 2, 0)
    assert app._handle_csi(f"<0;{col};{row}", "M") == []
    assert all(p == (10, 10, 10) for row in app._pixels for p in row)


def test_fill_tool_does_not_cross_a_wall():
    app = make_app()
    app._tool = TOOL_FILL
    for x in range(4):
        app._pixels[2][x] = (255, 0, 0)  # a wall across row 2
    app._color = (0, 255, 0)
    col, row = cell_screen(app, 1, 0)  # click above the wall (pixel (1,0))
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    assert {(c["x"], c["y"]) for c in changes} == {
        (x, y) for x in range(4) for y in range(2)
    }
    assert app._pixels[2][0] == (255, 0, 0)  # the wall survives
    assert app._pixels[3][3] == (10, 10, 10)  # below the wall untouched


def test_fill_tool_sends_single_batched_update():
    app = make_app()
    app._tool = TOOL_FILL
    app._color = (255, 0, 0)
    sent = []
    app._send_edit = lambda changes: sent.append(changes)
    col, row = cell_screen(app, 0, 0)
    changes = app._handle_csi(f"<0;{col};{row}", "M")
    app._after_edit(changes)
    assert len(changes) == 16  # one flat batch of every pixel
    assert len(sent) == 1  # exactly one edit message, not one per pixel
    assert len(sent[0]) == 16


def test_fill_drag_does_not_fill():
    app = make_app()
    app._tool = TOOL_FILL
    app._color = (255, 0, 0)
    col, row = cell_screen(app, 1, 0)
    assert app._handle_csi(f"<32;{col};{row}", "M") == []  # a drag, not a press
    assert app._pixels[0][0] == (10, 10, 10)


def test_fill_and_text_hotkeys_and_buttons_select():
    app = make_app()
    for hotkey, tool in (("b", TOOL_FILL), ("t", TOOL_TEXT)):
        app._handle_char(hotkey)
        assert app._tool == tool
    for ident, tool in (("fill", TOOL_FILL), ("text", TOOL_TEXT)):
        b = next(x for x in app._shape_toolbar_geometry()["buttons"] if x.ident == ident)
        app._handle_csi(f"<0;{b.col};{b.row}", "M")
        assert app._tool == tool


def test_top_line_shows_fill_and_text_labels():
    app = make_app()
    # The header word can truncate on a narrow terminal, but the right-aligned
    # shape buttons always render: check for the actual [Fill]/[Text] labels.
    app._tool = TOOL_FILL
    assert "\x1b[7m[Fill]" in app._render_top_line()  # active -> reversed video
    app._tool = TOOL_TEXT
    assert "\x1b[7m[Text]" in app._render_top_line()
    app._tool = TOOL_FILLED_RECT  # inactive buttons render plain
    assert "[Fill]" in app._render_top_line() and "[Text]" in app._render_top_line()


def test_fill_tool_space_fills_at_cursor():
    app = make_app()
    app._handle_char("b")
    app._color = (0, 255, 0)
    app._cursor_x, app._cursor_y = 1, 1
    changes = app._handle_char(" ")
    assert len(changes) == 16  # the whole background is one region
    assert app._pixels[3][3] == (0, 255, 0)


# --------------------------------------------------------------------------- #
# Text tool: 5x7 bitmap font + the session state machine
# --------------------------------------------------------------------------- #


def text_app(width=40, height=16, term=(120, 40)) -> CanvasApp:
    """A 40x16 canvas in a big terminal, so a full line of 5x7 text fits."""
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=term[0], height=term[1], force_interactive=True, legacy_windows=False,
    )
    app = CanvasApp(
        "/tmp/nonexistent.sock", background=(10, 10, 10),
        console=console, input_stream=io.StringIO(), size_provider=lambda: term,
    )
    app._blink_active = True
    app._apply({
        "type": "full",
        "width": width,
        "height": height,
        "background": [10, 10, 10],
        "pixels": [[[10, 10, 10]] * width for _ in range(height)],
    })
    return app


def test_font_covers_uppercase_lowercase_digits_and_punctuation():
    punctuation = set(".,-!?_()[]{}`~+*/\\|'\":;=<>^#%&@$")
    supported = set(_FONT5X7)
    assert set(string.ascii_uppercase) <= supported
    assert set(string.ascii_lowercase) <= supported
    assert set(string.digits) <= supported
    assert punctuation <= supported
    assert len(_FONT5X7) > 80  # a real font, not a stub


def test_font_every_glyph_is_5x7_with_a_lit_pixel():
    for char, pattern in _FONT5X7.items():
        assert len(pattern) == FONT_H == 7, char
        for row in pattern:
            assert len(row) == FONT_W == 5, char
            assert set(row) <= {".", "#"}, char
        assert any("#" in row for row in pattern), char
        assert glyph_pixels(char, 0, 0)  # renders at least one pixel


def test_glyph_pixels_positions_at_origin_and_offset():
    px = glyph_pixels("H", 3, 4)
    assert (3, 4) in px and (7, 4) in px  # first row, both lit corners
    assert (3, 10) in px and (7, 10) in px  # last row (4 + FONT_H - 1)
    assert all(3 <= x < 3 + FONT_W and 4 <= y < 4 + FONT_H for x, y in px)
    assert glyph_pixels(" ", 0, 0) == set()  # space has no glyph box


def test_text_click_places_insertion_point():
    app = text_app()
    app._tool = TOOL_TEXT
    col, row = cell_screen(app, 3, 2)  # cell (3,2) -> pixel (3,4)
    assert app._handle_csi(f"<0;{col};{row}", "M") == []
    assert app._text_active is True
    assert (app._text_x, app._text_y) == (3, 4)
    assert (app._cursor_x, app._cursor_y) == (3, 4)


def test_text_typing_draws_and_advances():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(2, 2)
    changes = app._handle_char("H")
    assert changes and all(c["color"] == [255, 0, 0] for c in changes)
    assert all(c["y"] in range(2, 2 + FONT_H) for c in changes)
    assert app._text_x == 2 + FONT_W + 1  # advanced past the 5-wide glyph
    app._handle_char("i")
    assert app._text_x == 2 + 2 * (FONT_W + 1)


def test_text_mouse_click_then_type():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (0, 0, 255)
    col, row = cell_screen(app, 0, 0)
    app._handle_csi(f"<0;{col};{row}", "M")  # place at (0,0)
    changes = app._handle_char("A")
    # "A" row 0 is ".###." so the lit top-left pixel is (1,0), not (0,0)
    assert (1, 0) in {(c["x"], c["y"]) for c in changes}


def test_text_backspace_erases_last_char():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(1, 1)
    app._handle_char("A")
    app._handle_char("B")
    before = app._text_x
    changes = app._handle_char("\x7f")  # backspace
    assert changes and all(c["color"] == [10, 10, 10] for c in changes)
    assert app._text_x == before - (FONT_W + 1)  # insertion stepped back
    # "A" row 0 is ".###." so at (1,1) the lit column is x=2 -> pixel (2,1)
    assert app._pixels[1][2] == (255, 0, 0)  # A is still there
    app._handle_char("\x7f")  # backspace again removes A
    assert app._text_x == 1
    assert app._pixels[1][2] == (10, 10, 10)


def test_text_escape_reverts_whole_session():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(2, 2)
    app._handle_char("H")
    app._handle_char("i")
    changes = app._read_escape_sequence()  # a stray ESC (no CSI) cancels
    assert changes and all(c["color"] == [10, 10, 10] for c in changes)
    assert app._text_active is False
    assert all(p == (10, 10, 10) for row in app._pixels for p in row)


def test_text_enter_moves_to_new_line():
    app = text_app(height=24)  # tall enough that y=10 line (rows 10..16) fits
    app._tool = TOOL_TEXT
    app._text_place(4, 2)
    app._handle_char("H")
    assert app._handle_char("\r") == []
    assert app._text_x == 4  # the new line starts at the session's start x
    assert app._text_y == 2 + FONT_H + 1


def test_text_enter_out_of_bounds_is_ignored():
    app = text_app(height=12)  # bottom edge at y=11
    app._tool = TOOL_TEXT
    app._text_place(2, 4)  # next line would need rows 12..18: no room
    assert app._handle_char("\r") == []
    assert (app._text_x, app._text_y) == (2, 4)


def test_text_out_of_bounds_right_edge_stops_accepting():
    app = text_app(width=20)
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(15, 2)  # 15 + FONT_W == 20: exactly fits
    assert app._handle_char("H")  # fits: drawn
    assert app._text_x == 15 + FONT_W + 1
    assert app._handle_char("I") == []  # would overflow: not accepted
    assert app._text_x == 15 + FONT_W + 1  # did not advance
    assert all(p == (10, 10, 10) for p in app._pixels[2][20:])  # nothing past


def test_text_switching_tool_finalizes_and_commits():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(2, 2)
    app._handle_char("H")
    app._handle_char("p")  # while typing, 'p' draws a glyph, it does NOT switch
    assert app._tool == TOOL_TEXT
    assert app._text_active is True
    b = app._shape_toolbar_geometry()["buttons"][0]  # the filled-rect button
    app._handle_csi(f"<0;{b.col};{b.row}", "M")  # switch tool via the mouse
    assert app._tool == TOOL_FILLED_RECT
    assert app._text_active is False  # finalized: nothing more to revert
    assert app._pixels[2][2] == (255, 0, 0)  # the typed text stays committed


def test_text_click_elsewhere_starts_a_fresh_session():
    app = text_app(height=24)  # the "E" at y=10 needs rows 10..16
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(2, 2)
    app._handle_char("H")
    col, row = cell_screen(app, 1, 5)  # click the canvas elsewhere
    app._handle_csi(f"<0;{col};{row}", "M")
    assert app._text_active is True
    assert (app._text_x, app._text_y) == (1, 10)
    assert app._pixels[2][2] == (255, 0, 0)  # the old text is committed
    app._handle_char("E")
    app._read_escape_sequence()  # Escape only reverts the NEW session
    assert app._pixels[2][2] == (255, 0, 0)  # old H remains
    assert app._pixels[10][1] == (10, 10, 10)  # new E reverted


def test_text_space_advances_without_drawing():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(0, 0)
    assert app._handle_char(" ") == []
    assert app._text_x == FONT_W + 1
    assert all(p == (10, 10, 10) for row in app._pixels for p in row)
    app._handle_char("\x7f")  # backspace undoes the space
    assert app._text_x == 0


def test_text_punctuation_renders():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    for ch in ".,!?-()":
        assert glyph_pixels(ch, 0, 0)  # every punctuation glyph has pixels
    app._text_place(0, 0)
    assert app._handle_char("!")  # '!' draws


def test_text_changes_are_sent_as_normal_edits():
    app = text_app()
    app._tool = TOOL_TEXT
    app._color = (255, 0, 0)
    app._text_place(0, 0)
    sent = []
    app._send_edit = lambda changes: sent.append(changes)
    changes = app._handle_char("H")
    app._after_edit(changes)
    assert len(sent) == 1
    assert all({"x", "y", "color"} <= set(c) for c in sent[0])  # server-ready dicts
