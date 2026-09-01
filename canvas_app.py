"""CorvusPixel interactive canvas — the window the *user* draws in.

This replaces the old passive renderer. It connects to the MCP server's
session-scoped Unix socket, renders the shared canvas live, and lets the user
draw:

- **Keyboard**: arrow keys move the cursor; ``space`` paints with the current
  brush (idempotently — painting the same pixel twice never toggles it back);
  ``x`` erases; ``e`` toggles the eraser tool; ``p`` returns to the paint tool;
  ``r``/``o``/``f``/``s``/``l`` select the filled-rect / hollow-rect /
  filled-square / hollow-square / line tools; ``b`` selects the bucket-fill
  tool (a click flood-fills the connected same-colour region); ``t`` selects
  the text tool (a click places a text caret, typing draws in a small bitmap
  font, backspace erases, Enter starts a new line, Escape reverts the whole
  session, and the brush-size bar / ``+``/``-`` keys set the text scale while
  the tool is active); ``a`` selects the label tool (a click places a
  terminal-cell caret, typing writes literal terminal characters in the
  terminal's own font — crisp at any size — over the pixel grid, backspace
  removes the last character, Enter starts a new line below, Escape cancels the
  whole in-progress label, and clicking elsewhere or switching tools finalizes
  it); ``+``/``-`` grow/shrink the square brush; ``[``/``]`` and
  ``{``/``}`` grow/shrink the canvas (columns at the right edge, rows at the
  bottom edge); ``c`` cycles the palette; ``1``-``8`` pick a palette color;
  ``Tab`` opens the visual palette (arrow keys + Enter/space select); ``q`` or
  Ctrl+C quits.
- **Mouse**: a clickable toolbar above the canvas — buttons for paint, eraser,
  brush size, palette, column/row resize and quit, plus a Paint-style brush-size
  bar (click it, or drag the ``●`` handle, to size the brush). Seven drawing
  tools (filled/hollow rectangle and square, a straight line, bucket fill and
  text) live at the right-hand end of the top row: click one to select it.
  Shape tools then click-drag on the canvas — a dimmed preview follows the
  cursor and is committed on release (Escape cancels). The bucket fill paints
  the connected same-colour region in one click. The text tool places a caret
  where you click; typing draws each character in the current colour (the
  brush-size bar doubles as the text-scale control while this tool is active),
  backspace erases the last character, Enter starts a new line below, and
  Escape reverts everything typed in this session. The label tool (also in the
  top-right corner) is the literal-text counterpart: click to place a
  terminal-cell caret, type to write real terminal characters over the pixel
  grid in the current colour, backspace removes the last character, Enter
  starts a new line below, Escape cancels the whole in-progress label, and
  clicking elsewhere or switching tools finalizes it. Clicking (or
  click-dragging) on the canvas
  paints with the current brush; clicking a palette swatch selects that color.
  A second row of common-colour swatches sits at the bottom, ending in a
  rainbow "custom colour" swatch that opens a small hex input (type
  ``#rrggbb`` digits, Enter to pick, Esc to cancel).
- **Layout**: the canvas is centered in the terminal (horizontally and
  vertically) and re-centered when the terminal is resized (SIGWINCH). The
  cursor is a blinking reverse-video block framed by ``[ ]`` brackets so its
  position is unambiguous even on an empty canvas (where reverse video would be
  invisible because fg == bg). A compact keyboard-shortcut panel sits in the
  bottom-left margin of the chrome, packed into labelled groups (move / draw /
  brush / shapes / canvas / other); it shrinks on narrow windows and disappears
  if there is no room.

Rendering: each logical pixel is drawn as ``CELL_W`` (2) terminal columns of a
half-block ``▀``, so one display cell spans two canvas rows *and* two terminal
columns — pixels read as squares on the common ~1:2 terminal fonts. Diff-only
repainting is unchanged: only the cells whose content changed are rewritten.

Every user edit is applied locally for instant feedback (no round-trip wait)
*and* written back to the server as ``{"type": "edit", "changes": [...]}``.
Canvas size changes go back as ``{"type": "resize", "width", "height"}``; the
server keeps its canvas (and ``see_canvas``) in sync.

Why raw ANSI instead of textual? The canvas is a grid of cells that must repaint
only the cells that changed — no full-region redraws. That is the core rendering
requirement here, and it is simplest with our own ANSI cell-diff on top of
rich's ``Live`` lifecycle context. Terminal input (arrow keys, SGR mouse) arrives
as byte sequences on stdin, which we parse directly; pulling in a full widget
framework (textual) would add a large dependency without helping the diff-only
renderer. The toolbar buttons and the brush bar are custom clickable regions hit-
tested over the same SGR mouse reports the canvas uses — a small button/slider
model (``ToolButton`` / ``BrushSlider``) with an ``on_click``-style action
dispatch, so UI controls and mouse drawing share one input path.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console
from rich.live import Live
from rich.text import Text

# --------------------------------------------------------------------------- #
# Types / protocol helpers
# --------------------------------------------------------------------------- #

Color = tuple[int, int, int]
Cell = tuple[Color, Color]  # (top pixel, bottom pixel) for one display cell

RESET = "\x1b[0m"
HALF_BLOCK = "▀"

# Each logical pixel is CELL_W terminal columns wide (2). Combined with the
# half-block vertical split (each pixel is also half a terminal row tall), a
# pixel is CELL_W x 0.5 terminal cells — square on the common ~1:2 fonts.
CELL_W = 2
CELL_CH = HALF_BLOCK * CELL_W  # the characters that draw one logical pixel

BRUSH_MIN = 1
BRUSH_MAX = 7  # square brush side lengths: 1x1 .. 7x7
MIN_CANVAS = 1
MAX_CANVAS = 100

DEFAULT_SOCKET = os.environ.get("CORVUSPIXEL_SOCK", "/tmp/corvuspixel.sock")

# The brush palette: (name, color). Keys 1-8 select these; 'c' cycles; the
# on-screen swatches let you pick with arrows (Tab) or a mouse click.
PALETTE: list[tuple[str, Color]] = [
    ("white", (255, 255, 255)),
    ("black", (0, 0, 0)),
    ("red", (255, 0, 0)),
    ("yellow", (255, 214, 64)),
    ("green", (0, 255, 0)),
    ("cyan", (0, 255, 255)),
    ("blue", (0, 0, 255)),
    ("pink", (255, 120, 150)),
]

# A second row of everyday colours always visible at the bottom of the window
# (clickable, like the palette swatches) plus a rainbow "custom colour" swatch
# that opens a hex input. Keeping both rows separate from the 8-key palette.
QUICK_COLORS: list[tuple[str, Color]] = [
    ("orange", (255, 140, 0)),
    ("purple", (128, 0, 128)),
    ("teal", (0, 128, 128)),
    ("brown", (139, 69, 19)),
    ("gray", (128, 128, 128)),
    ("maroon", (128, 0, 0)),
    ("olive", (128, 128, 0)),
    ("navy", (0, 0, 128)),
]


class ToolButton:
    """A clickable toolbar button: a labelled region with an on-click action."""

    __slots__ = ("ident", "label", "action", "row", "col", "width")

    def __init__(self, ident: str, label: str, action: str, row: int, col: int) -> None:
        self.ident = ident
        self.label = label
        self.action = action  # name of a CanvasApp method to call on click
        self.row = row
        self.col = col
        self.width = len(label)

    def contains(self, col: int, row: int) -> bool:
        return row == self.row and self.col <= col < self.col + self.width


class BrushSlider:
    """A Paint-style brush-size bar: a track with a handle showing the size.

    Clicking the track (or dragging the handle) sets the brush size; the track
    plus one column of margin on each side is the clickable zone, so dragging
    past either end clamps to the min/max instead of dropping the drag.
    """

    __slots__ = ("row", "col", "width")

    def __init__(self, row: int, col: int) -> None:
        self.row = row
        self.col = col
        self.width = BRUSH_MAX  # one track cell per brush size

    def contains(self, col: int, row: int) -> bool:
        return row == self.row and (self.col - 1) <= col < (self.col + self.width + 1)

    def brush_size_for(self, col: int) -> int:
        pos = min(max(col, self.col), self.col + self.width - 1)
        return max(BRUSH_MIN, min(BRUSH_MAX, pos - self.col + 1))


# The toolbar is a single row of clickable buttons declared in one place, so the
# renderer and the mouse hit-testing both come from the same spec. ``action`` is
# the name of a CanvasApp method to invoke on click; the slider is special.
# Each entry carries both a text label and a single-width Unicode icon; which
# one renders depends on the terminal's Unicode support (``_use_icons``).
_TOOLBAR_SPEC: list[tuple[str, str, str, str]] = [
    ("paint", "[Paint]", "●", "_tool_paint"),
    ("eraser", "[Eraser]", "▨", "_tool_eraser"),
    ("brush_dec", "[-]", "−", "_tool_brush_dec"),
    ("brush_slider", "", "", "brush_slider"),
    ("brush_inc", "[+]", "+", "_tool_brush_inc"),
    ("palette", "[Palette]", "◉", "_tool_palette"),
    ("col_dec", "[W-]", "◀", "_tool_col_dec"),
    ("col_inc", "[W+]", "▶", "_tool_col_inc"),
    ("row_dec", "[H-]", "▲", "_tool_row_dec"),
    ("row_inc", "[H+]", "▼", "_tool_row_inc"),
    ("quit", "[Quit]", "✕", "_tool_quit"),
]

# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

TOOL_PAINT = "paint"
TOOL_ERASER = "eraser"
TOOL_FILLED_RECT = "filled_rect"
TOOL_FILLED_SQUARE = "filled_square"
TOOL_HOLLOW_RECT = "hollow_rect"
TOOL_HOLLOW_SQUARE = "hollow_square"
TOOL_LINE = "line"
TOOL_FILL = "fill"
TOOL_TEXT = "text"
TOOL_LABEL = "label"
TOOL_SELECT = "select"

SHAPE_TOOLS = (
    TOOL_FILLED_RECT,
    TOOL_FILLED_SQUARE,
    TOOL_HOLLOW_RECT,
    TOOL_HOLLOW_SQUARE,
    TOOL_LINE,
)

_TOOL_LABELS: dict[str, str] = {
    TOOL_PAINT: "paint",
    TOOL_ERASER: "eraser",
    TOOL_FILLED_RECT: "filled",
    TOOL_FILLED_SQUARE: "filled-sq",
    TOOL_HOLLOW_RECT: "hollow",
    TOOL_HOLLOW_SQUARE: "hollow-sq",
    TOOL_LINE: "line",
    TOOL_FILL: "fill",
    TOOL_TEXT: "text",
    TOOL_LABEL: "label",
    TOOL_SELECT: "select",
}

# The drawing tools, shown right-aligned on the top (header) row — a distinct
# region from the centered toolbar. Same (ident, label, icon, action) convention
# as ``_TOOLBAR_SPEC`` so rendering and hit-testing share one spec. The fill,
# text, label and select tools are click tools, not click-drag shapes, so they
# are NOT in ``SHAPE_TOOLS`` (that tuple drives the drag -> preview -> commit
# state machine for the pixel shapes; the select tool has its own label drag).
_SHAPE_SPEC: list[tuple[str, str, str, str]] = [
    ("filled_rect", "[FR]", "▬", "_tool_filled_rect"),
    ("filled_square", "[FS]", "■", "_tool_filled_square"),
    ("hollow_rect", "[HR]", "▭", "_tool_hollow_rect"),
    ("hollow_square", "[HS]", "□", "_tool_hollow_square"),
    ("line", "[Line]", "╱", "_tool_line"),
    ("fill", "[Fill]", "▩", "_tool_fill"),
    ("text", "[Text]", "A", "_tool_text"),
    ("label", "[Label]", "⌨", "_tool_label"),
    ("select", "[Sel]", "⌖", "_tool_select"),
]

# The keyboard-shortcut help panel in the bottom-left margin of the chrome:
# (group header, keys). Packed into the palette/quick-colour rows' left margin
# (``_shortcuts_panel``), most useful groups first; see the panel's docstring.
_SHORTCUT_GROUPS: list[tuple[str, list[str]]] = [
    ("Move", ["←↑↓→"]),
    ("Draw", ["space", "x", "e"]),
    ("Brush", ["+", "−"]),
    ("Select", ["v", "Del", "Enter"]),
    ("Shapes", ["p", "r", "o", "f", "s", "l", "b", "t", "a"]),
    ("Canvas", ["[ ]", "{ }", "Tab"]),
    ("Other", ["c", "1-8", "q"]),
]

# In-progress shapes preview at 40% of the tool colour so they clearly read as
# uncommitted ghosts.
PREVIEW_DIM = 0.4


# --------------------------------------------------------------------------- #
# Shape geometry (pure functions, no app state — directly testable)
# --------------------------------------------------------------------------- #


def bresenham(x1: int, y1: int, x2: int, y2: int) -> list[tuple[int, int]]:
    """Every pixel on the line (x1,y1)->(x2,y2), Bresenham, unbounded."""
    out: list[tuple[int, int]] = []
    dx = abs(x2 - x1)
    dy = -abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx + dy
    while True:
        out.append((x1, y1))
        if x1 == x2 and y1 == y2:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x1 += sx
        if e2 <= dx:
            err += dx
            y1 += sy
    return out


def fill_rect_cells(
    x1: int, y1: int, x2: int, y2: int
) -> set[tuple[int, int]]:
    """Every cell in the inclusive bounding box (x1,y1)-(x2,y2)."""
    return {
        (x, y)
        for x in range(min(x1, x2), max(x1, x2) + 1)
        for y in range(min(y1, y2), max(y1, y2) + 1)
    }


def hollow_rect_cells(
    x1: int, y1: int, x2: int, y2: int, thickness: int
) -> set[tuple[int, int]]:
    """The border ring ``thickness`` cells thick around the inclusive box."""
    xa, xb = min(x1, x2), max(x1, x2)
    ya, yb = min(y1, y2), max(y1, y2)
    t = max(1, thickness)
    return {
        (x, y)
        for x in range(xa, xb + 1)
        for y in range(ya, yb + 1)
        if (x - xa) < t or (xb - x) < t or (y - ya) < t or (yb - y) < t
    }


def stamp_cells(cx: int, cy: int, size: int) -> set[tuple[int, int]]:
    """The square brush centred on (cx, cy) — unbounded, like brush math."""
    n = max(1, size)
    half = n // 2
    return {
        (x, y)
        for y in range(cy - half, cy - half + n)
        for x in range(cx - half, cx - half + n)
    }


def thick_line_cells(
    x1: int, y1: int, x2: int, y2: int, thickness: int
) -> set[tuple[int, int]]:
    """A line of the given thickness: the brush stamped along the centre line."""
    cells: set[tuple[int, int]] = set()
    for cx, cy in bresenham(x1, y1, x2, y2):
        cells.update(stamp_cells(cx, cy, thickness))
    return cells


def square_end(
    start: tuple[int, int], end: tuple[int, int]
) -> tuple[int, int]:
    """Snap ``end`` so the box from ``start`` is a square (start stays a corner)."""
    (x1, y1), (x2, y2) = start, end
    dx = 1 if x2 >= x1 else -1
    dy = 1 if y2 >= y1 else -1
    side = max(abs(x2 - x1), abs(y2 - y1))
    return x1 + dx * side, y1 + dy * side


def parse_hex(color: str) -> Color:
    """Parse a ``#rrggbb`` color string into an ``(r, g, b)`` tuple."""
    value = color.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"color must be '#rrggbb', got {color!r}")
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        raise ValueError(f"invalid hex color {color!r}") from None


def hex_str(color: Color) -> str:
    """Render an ``(r, g, b)`` tuple back to a ``#rrggbb`` string."""
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


# --------------------------------------------------------------------------- #
# Bucket fill (pure function, no app state — directly testable)
# --------------------------------------------------------------------------- #


def flood_fill_region(
    grid: list[list[Color]], x: int, y: int
) -> set[tuple[int, int]]:
    """Every pixel 4-connected to (x, y) that shares grid[y][x]'s colour.

    Iterative (explicit stack), so a 100x100 canvas — up to 10,000 connected
    pixels — never hits Python's recursion limit. ``grid`` is a 2-D list of
    colour tuples (the app's ``_pixels`` shape). Out-of-bounds starts return an
    empty set.
    """
    height = len(grid)
    if height == 0:
        return set()
    width = len(grid[0])
    if not (0 <= x < width and 0 <= y < height):
        return set()
    target = grid[y][x]
    region: set[tuple[int, int]] = set()
    stack: list[tuple[int, int]] = [(x, y)]
    while stack:
        cx, cy = stack.pop()
        if (cx, cy) in region:
            continue
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        if grid[cy][cx] != target:
            continue  # a different colour is a wall for the fill
        region.add((cx, cy))
        stack.append((cx - 1, cy))
        stack.append((cx + 1, cy))
        stack.append((cx, cy - 1))
        stack.append((cx, cy + 1))
    return region


# --------------------------------------------------------------------------- #
# Text tool: a tiny 5x7 bitmap font + pure pixel math (directly testable)
# --------------------------------------------------------------------------- #

# Each character is 7 rows of exactly 5 columns; '#' = lit pixel, '.' = off.
# Covers uppercase, lowercase, digits and basic punctuation; space is handled
# specially by the text tool (advance only, no pixels).
FONT_W = 5
FONT_H = 7
FONT_SPACING = 1   # terminal columns between characters
FONT_LINE_SPACING = 1  # rows between lines

_FONT5X7: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "a": (".....", ".....", ".###.", "....#", ".####", "#...#", ".####"),
    "b": ("#....", "#....", "####.", "#...#", "#...#", "#...#", "####."),
    "c": (".....", ".....", ".###.", "#....", "#....", "#...#", ".###."),
    "d": ("....#", "....#", ".####", "#...#", "#...#", "#...#", ".####"),
    "e": (".....", ".....", ".###.", "#...#", "#####", "#....", ".####"),
    "f": ("..##.", ".#...", ".#...", "####.", ".#...", ".#...", ".#..."),
    "g": (".....", ".####", "#...#", "#...#", ".####", "....#", ".###."),
    "h": ("#....", "#....", "####.", "#...#", "#...#", "#...#", "#...#"),
    "i": ("..#..", ".....", ".##..", "..#..", "..#..", "..#..", ".###."),
    "j": ("...#.", ".....", "..##.", "...#.", "...#.", "...#.", "#..#."),
    "k": ("#....", "#....", "#..#.", "#.#..", "##...", "#.#..", "#..##"),
    "l": (".##..", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "m": (".....", ".....", "##.#.", "#.#.#", "#.#.#", "#.#.#", "#...#"),
    "n": (".....", ".....", "####.", "#...#", "#...#", "#...#", "#...#"),
    "o": (".....", ".....", ".###.", "#...#", "#...#", "#...#", ".###."),
    "p": (".....", "####.", "#...#", "#...#", "####.", "#....", "#...."),
    "q": (".....", ".####", "#...#", "#...#", ".####", "....#", "....#"),
    "r": (".....", ".....", "#.##.", "##..#", "#....", "#....", "#...."),
    "s": (".....", ".....", ".####", "#....", ".###.", "....#", "####."),
    "t": (".#...", ".#...", "####.", ".#...", ".#...", ".#...", "..##."),
    "u": (".....", ".....", "#...#", "#...#", "#...#", "#...#", ".####"),
    "v": (".....", ".....", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "w": (".....", ".....", "#...#", "#...#", "#.#.#", "#.#.#", "##.##"),
    "x": (".....", ".....", "#...#", ".#.#.", "..#..", ".#.#.", "#...#"),
    "y": (".....", "#...#", "#...#", "#...#", ".####", "....#", ".###."),
    "z": (".....", ".....", "#####", "...#.", "..#..", ".#...", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "....#", "...#.", ".###.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    ".": (".....", ".....", ".....", ".....", ".....", "..#..", "..#.."),
    ",": (".....", ".....", ".....", ".....", "..#..", "..#..", ".#..."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "[": ("..##.", "..#..", "..#..", "..#..", "..#..", "..#..", "..##."),
    "]": (".##..", "..#..", "..#..", "..#..", "..#..", "..#..", ".##.."),
    "{": ("...#.", "..#..", "..#..", ".##..", "..#..", "..#..", "...#."),
    "}": (".#...", "..#..", "..#..", "..##.", "..#..", "..#..", ".#..."),
    "/": ("....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."),
    "\\": ("#....", ".#...", ".#...", "..#..", "...#.", "...#.", "....#"),
    "|": ("..#..", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "*": (".....", "#.#.#", ".#.#.", "#####", ".#.#.", "#.#.#", "....."),
    ":": (".....", "..#..", "..#..", ".....", "..#..", "..#..", "....."),
    ";": (".....", "..#..", "..#..", ".....", "..#..", "..#..", ".#..."),
    "'": ("..#..", "..#..", ".....", ".....", ".....", ".....", "....."),
    '"': (".#.#.", ".#.#.", ".....", ".....", ".....", ".....", "....."),
    "#": (".#.#.", "#####", ".#.#.", ".#.#.", "#####", ".#.#.", "....."),
    "$": ("..#..", ".####", "#..#.", ".###.", "..#.#", "####.", "..#.."),
    "%": ("##...", "##..#", "...#.", "..#..", ".#...", "#..##", "...##"),
    "&": (".##..", "#..#.", "#.#..", ".##..", "#.#.#", "#..#.", ".##.#"),
    "@": (".###.", "#...#", "#.###", "#.###", "#.###", "#....", ".###."),
    "<": ("...#.", "..#..", ".#...", ".#...", "..#..", "...#.", "....."),
    ">": (".#...", "..#..", "...#.", "...#.", "..#..", ".#...", "....."),
    "^": ("..#..", ".#.#.", "#...#", ".....", ".....", ".....", "....."),
    "~": (".....", ".....", ".##.#", "#.##.", ".....", ".....", "....."),
    "`": (".#...", "..#..", ".....", ".....", ".....", ".....", "....."),
}


def glyph_pixels(char: str, x: int, y: int, scale: int = 1) -> set[tuple[int, int]]:
    """The lit pixels of ``char`` drawn with its top-left corner at (x, y).

    ``scale`` replicates each lit font pixel as an N x N block of canvas pixels
    (nearest-neighbour pixel-art upscaling); scale 1 is the native 5x7 glyph.
    A scale below 1 renders nothing.
    """
    pattern = _FONT5X7.get(char)
    if pattern is None or scale < 1:
        return set()
    return {
        (x + fx * scale + i, y + fy * scale + j)
        for fy, row in enumerate(pattern)
        for fx, cell in enumerate(row)
        if cell == "#"
        for i in range(scale)
        for j in range(scale)
    }


# --------------------------------------------------------------------------- #
# Objects: shapes, fills, pixel-text and labels share one object model
# --------------------------------------------------------------------------- #

# Every committed shape, bucket fill, pixel-art text session and terminal-text
# label becomes ONE object: an id, a kind, a single colour, metadata (``data``)
# and — for pixel objects — the exact set of canvas pixels it owns. Objects sit
# on top of the base raster (freehand paint/eraser) and render in z-order = list
# order, so later objects cover earlier ones. Labels are the exception: they are
# a terminal overlay (kind "label", no pixels) drawn over the composite. The
# object itself is what syncs to the server and what see_canvas shows.
#
# ``data`` carries the type-specific metadata that makes an object re-appliable
# and human-readable: a shape's kind/bounds/thickness, a fill's pixel count, a
# text session's characters + glyph positions, a label's per-line (row, col,
# text). For labels ``data["lines"]`` is in terminal-cell coordinates (NOT
# canvas pixels); the hitbox — the bounding rectangle covering every character
# cell — is computed on demand, and the select tool uses it for hit-testing,
# move and delete.


@dataclass
class Object:
    """One committed drawing, in z-order = ``_objects`` list order."""
    oid: int
    kind: str            # "shape" | "fill" | "text" | "label"
    color: Color
    data: dict[str, Any]
    pixels: set[tuple[int, int]] = field(default_factory=set)  # empty for labels


# The selection border draws in this bright colour so it stands out against
# both the pixel grid and any label text around it.
SELECTION_COLOR: Color = (0, 255, 255)


def label_cells(obj: Object) -> dict[tuple[int, int], tuple[str, Color]]:
    """Expand a label object into its per-cell overlay:
    ``{(row, col): (char, color)}`` for every character it renders."""
    out: dict[tuple[int, int], tuple[str, Color]] = {}
    for row, col, text in obj.data["lines"]:
        for i, char in enumerate(text):
            out[(row, col + i)] = (char, obj.color)
    return out


def label_hitbox(obj: Object) -> tuple[int, int, int, int] | None:
    """The bounding rectangle ``(row_min, col_min, row_max, col_max)`` covering
    every character cell the label renders, or None for an empty label."""
    cells = label_cells(obj)
    if not cells:
        return None
    rows = [r for r, _c in cells]
    cols = [c for _r, c in cells]
    return min(rows), min(cols), max(rows), max(cols)


def hitbox_from_cells(
    cells: dict[tuple[int, int], tuple[str, Color]],
) -> tuple[int, int, int, int] | None:
    """The bounding rectangle over a set of overlay cells, or None if empty."""
    if not cells:
        return None
    rows = [r for r, _c in cells]
    cols = [c for _r, c in cells]
    return min(rows), min(cols), max(rows), max(cols)


def label_border_cells(
    hitbox: tuple[int, int, int, int],
) -> dict[tuple[int, int], str]:
    """The selection-border ring around a hitbox: ``{(row, col): box char}``.

    One cell outside the rectangle on every side (corners included), so the
    border wraps the label without covering any of its characters."""
    r0, c0, r1, c1 = hitbox
    cells: dict[tuple[int, int], str] = {}
    for c in range(c0 - 1, c1 + 2):
        cells[(r0 - 1, c)] = "─"
        cells[(r1 + 1, c)] = "─"
    for r in range(r0, r1 + 1):
        cells[(r, c0 - 1)] = "│"
        cells[(r, c1 + 1)] = "│"
    cells[(r0 - 1, c0 - 1)] = "┌"
    cells[(r0 - 1, c1 + 1)] = "┐"
    cells[(r1 + 1, c0 - 1)] = "└"
    cells[(r1 + 1, c1 + 1)] = "┘"
    return cells


def pixel_bounds(pixels: set[tuple[int, int]]) -> tuple[int, int, int, int] | None:
    """The inclusive bounding box ``(x_min, y_min, x_max, y_max)`` over a set of
    canvas pixels, or None for an empty set."""
    if not pixels:
        return None
    xs = [x for x, _y in pixels]
    ys = [y for _x, y in pixels]
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
# The interactive canvas app
# --------------------------------------------------------------------------- #


class CanvasApp:
    """Draws the canvas and turns keyboard/mouse input into pixel edits.

    ``console``, ``input_stream`` and ``size_provider`` are injectable so tests
    can capture output, drive input and pin the terminal dimensions without a
    real terminal.
    """

    def __init__(
        self,
        socket_path: str,
        background: Color = (0, 0, 0),
        console: Console | None = None,
        input_stream: Any = None,
        size_provider: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._background = background
        self._width = 0
        self._height = 0
        self._pixels: list[list[Color]] = []
        self._connected = False

        self._cursor_x = 0
        self._cursor_y = 0
        self._palette_idx = 0
        self._palette_active = True  # whether the palette (not quick row) is the source
        self._quick_idx: int | None = None
        self._color = PALETTE[0][1]
        self._palette_mode = False
        self._custom_color_mode = False
        self._hex_buffer = ""
        self._tool = TOOL_PAINT
        self._brush_size = 1
        self._text_scale = 1  # text renders each font pixel as an NxN block
        self._pending_resize: tuple[int, int] | None = None

        # In-progress shape drag: start/end canvas cells and the local (never
        # sent) preview overlay. Empty until a shape tool is dragged.
        self._shape_drag: tuple[int, int] | None = None
        self._shape_end: tuple[int, int] | None = None
        self._preview_pixels: dict[tuple[int, int], Color] = {}

        # In-progress text entry: the insertion point, the x every new line
        # starts at, the glyphs typed so far (for backspace), the session's
        # colour (fixed at placement, so a text object always has one colour)
        # and every pixel the session has drawn so far. The session renders as a
        # live overlay and becomes ONE object when finalized; Escape drops it.
        # ``_text_edit_oid`` is the object being re-opened by the select tool's
        # Enter (it renders via the session instead of in place);
        # ``_text_edit_original`` is a copy of its pre-edit content so Escape
        # reverts to it. Empty until the text tool is clicked on the canvas.
        self._text_active = False
        self._text_x = 0
        self._text_y = 0
        self._text_line_start_x = 0
        self._text_history: list[dict[str, Any]] = []
        self._text_color: Color = self._color
        self._text_session_pixels: set[tuple[int, int]] = set()
        self._text_edit_oid: int | None = None
        self._text_edit_original: Object | None = None

        # Stored objects: every committed shape, bucket fill, pixel-art text
        # session and terminal-text label, in z-order = list order (later
        # objects render on top). Kept in sync with the server. ``_selected_id``
        # is the select tool's single selection (only one object at a time).
        self._objects: list[Object] = []
        self._object_next_id = 1
        self._selected_id: int | None = None
        # In-progress label entry (terminal cells, not pixels). The session's
        # typed cells render on top of the stored objects and only become
        # permanent (a brand-new object, or an in-place edit of the object being
        # edited) when the session is finalized. ``_label_color`` is fixed at
        # the session start, so a label object always has a single colour.
        self._label_active = False
        self._label_row = 0
        self._label_col = 0
        self._label_line_start_col = 0
        self._label_color: Color = self._color
        self._label_history: list[dict[str, Any]] = []
        self._label_session_cells: dict[tuple[int, int], tuple[str, Color]] = {}
        # In-place editing of an existing label (the select tool's Enter):
        # ``_label_edit_oid`` is the object being edited and
        # ``_label_edit_original`` is a copy of its pre-edit content so Escape
        # reverts to it. Both are None for a fresh label session.
        self._label_edit_oid: int | None = None
        self._label_edit_original: Object | None = None
        # Select-tool drag state for moving an object: which object is being
        # dragged, where the press landed relative to its hitbox's top-left, and
        # the dimmed preview (labels use an overlay preview object; pixel
        # objects use a shifted pixel set rendered via ``_preview_pixels``).
        self._drag_id: int | None = None
        self._drag_offset: tuple[int, int] | None = None
        self._label_drag_preview: Object | None = None
        self._object_drag_offset: tuple[int, int] | None = None
        self._object_drag_preview: set[tuple[int, int]] | None = None
        # The current on-screen overlay (stored labels + session + drag preview
        # + selection border), diffed against ``_old_label_chars`` so label
        # edits rewrite only the affected terminal columns.
        self._label_chars: dict[tuple[int, int], tuple[str, Color]] = {}
        self._old_label_chars: dict[tuple[int, int], tuple[str, Color]] = {}
        # The last wire representation we synced (or received), so redundant
        # ``_sync_objects`` calls don't re-broadcast an unchanged object list.
        self._last_objects_wire: list[dict[str, Any]] | None = None

        self._sock: socket.socket | None = None
        self._quit = threading.Event()
        self._lock = threading.RLock()
        self._input_stream = input_stream if input_stream is not None else sys.stdin
        self._old_termios: Any = None
        self._size_provider = size_provider

        self._console = console or Console(color_system="truecolor")
        self._icons = self._use_icons()
        # What is currently drawn on screen (all coordinates absolute, centered).
        self._layout_info: dict[str, int] | None = None
        self._force_full = True
        self._old_cells: list[list[Cell]] = []
        self._old_header = ""
        self._old_toolbar = ""
        self._pending_chrome = False  # next input only touches the chrome, not canvas
        self._old_palette_key: tuple[Any, ...] | None = None
        self._old_quick_key: tuple[Any, ...] | None = None
        self._old_panel: tuple[Any, ...] | None = None
        self._old_cursor_cell: tuple[int, int] | None = None
        self._cursor_was_reversed = False
        self._blink_active = self._blink_on()

    # -- layout --------------------------------------------------------------

    def _query_size(self) -> tuple[int, int]:
        """Terminal size as (columns, lines); tests inject a provider."""
        if self._size_provider is not None:
            return self._size_provider()
        size = shutil.get_terminal_size()
        return size.columns, size.lines

    def _use_icons(self) -> bool:
        """Whether the toolbar should render Unicode icons instead of text labels.

        We don't probe the terminal's font (there is no reliable way to); we use
        the usual heuristics: an explicit ``CORVUSPIXEL_ICONS=0/1`` override wins,
        otherwise we need a UTF-8 output encoding and a terminal that usually has
        the glyphs (not the Linux console or a dumb terminal). Tests pass a
        StringIO console with no encoding, so they fall back to text labels.
        """
        override = os.environ.get("CORVUSPIXEL_ICONS")
        if override is not None:
            return override.strip().lower() not in ("", "0", "false", "no", "off")
        encoding = (getattr(self._console.file, "encoding", None) or "").lower()
        if "utf" not in encoding:
            return False
        term = os.environ.get("TERM", "")
        if term in ("dumb", "linux") or term.startswith("cons"):
            return False
        return True

    def _compute_layout(self) -> dict[str, int]:
        """Where everything sits: centering offsets for the current terminal.

        The canvas is clamped to the space that fits: ``chrome_rows`` rows are
        always reserved for the header, toolbar, palette indicator + swatches,
        and the quick-colour row, so the whole stack never overflows the window.
        A canvas larger than the window shows its top rows (the bottom scrolls
        out) and clips horizontally — it never wraps or scrolls the terminal.
        """
        tw, th = self._query_size()
        chrome_rows = 5  # header + toolbar + palette indicator + palette + quick
        wanted = (self._height + 1) // 2  # ceil(height / 2)
        canvas_rows = max(1, min(wanted, th - chrome_rows))
        content_h = chrome_rows + canvas_rows
        top_pad = max(0, (th - content_h) // 2)
        left_pad = max(0, (tw - max(1, self._width) * CELL_W) // 2)
        return {
            "term_w": tw,
            "term_h": th,
            "canvas_rows": canvas_rows,
            "content_h": content_h,
            "top_pad": top_pad,
            "left_pad": left_pad,
        }

    def _layout(self) -> dict[str, int]:
        """The layout currently drawn (computes it on first use)."""
        if self._layout_info is None:
            self._layout_info = self._compute_layout()
        return self._layout_info

    def _blink_on(self) -> bool:
        """Cursor blink phase — toggles ~every half second."""
        return int(time.monotonic() * 2.0) % 2 == 0

    def _install_winch_handler(self) -> None:
        """Re-center on terminal resize. Best-effort (main thread only)."""
        try:
            signal.signal(signal.SIGWINCH, lambda _s, _f: self._mark_layout_dirty())
        except (ValueError, AttributeError, OSError):
            pass

    def _mark_layout_dirty(self) -> None:
        self._force_full = True

    def _pump(self) -> None:
        """Periodic upkeep: cursor blink + terminal-resize repaint."""
        blink = self._blink_on()
        if blink != self._blink_active or self._force_full:
            self._blink_active = blink
            self._draw()

    # -- socket message handling ---------------------------------------------

    def _apply(self, message: dict[str, Any]) -> None:
        with self._lock:
            msg_type = message.get("type")
            if msg_type == "full":
                self._resize(
                    message["width"],
                    message["height"],
                    tuple(message.get("background", self._background)),
                )
                for y in range(min(self._height, len(message["pixels"]))):
                    row = message["pixels"][y]
                    for x in range(min(self._width, len(row))):
                        self._pixels[y][x] = tuple(row[x])
                self._apply_objects(message.get("objects", []))
            elif msg_type == "update":
                if not self._pixels:
                    return
                for change in message["changes"]:
                    x, y = change["x"], change["y"]
                    if 0 <= x < self._width and 0 <= y < self._height:
                        self._pixels[y][x] = tuple(change["color"])
            elif msg_type == "objects":
                self._apply_objects(message.get("objects", []))
            else:
                return
            self._draw()

    def _resize(self, width: int, height: int, background: Color) -> None:
        self._width, self._height = width, height
        self._background = background
        self._pixels = [[background] * width for _ in range(height)]
        self._clip_objects_to_canvas()
        self._cursor_x = min(self._cursor_x, max(0, width - 1))
        self._cursor_y = min(self._cursor_y, max(0, height - 1))

    def _clip_objects_to_canvas(self) -> None:
        """Clip every pixel object to the current canvas, dropping empties.
        Labels are an overlay and are left untouched."""
        kept: list[Object] = []
        for obj in self._objects:
            if obj.kind != "label":
                obj.pixels = {
                    (x, y) for (x, y) in obj.pixels
                    if 0 <= x < self._width and 0 <= y < self._height
                }
                if not obj.pixels:
                    continue
            kept.append(obj)
        self._objects = kept

    # -- drawing ---------------------------------------------------------------

    def _render_color(self, x: int, y: int) -> Color:
        """The colour shown at canvas pixel (x, y): the in-progress text
        session, then stored objects in z-order (later = on top), then the base
        raster (freehand paint/eraser).

        The object being moved by the select tool and the one being edited by
        the text tool render via their previews/sessions instead, so they are
        skipped here and shown at the preview position."""
        if self._text_active and (x, y) in self._text_session_pixels:
            return self._text_color
        for obj in reversed(self._objects):
            if obj.kind == "label":
                continue
            if self._drag_id is not None and obj.oid == self._drag_id:
                continue
            if self._text_edit_oid is not None and obj.oid == self._text_edit_oid:
                continue
            if (x, y) in obj.pixels:
                return obj.color
        return self._pixels[y][x]

    def _display_rows(self) -> list[list[Cell]]:
        """Current desired screen content: one cell per (2 vertical pixels).

        Only the rows that fit in the visible canvas area are returned; a
        canvas taller than the window shows its top rows. An in-progress shape
        drag overlays its preview pixels here (local only, never committed);
        stored objects composite over the base raster via ``_render_color``."""
        layout = self._layout()
        rows: list[list[Cell]] = []
        for y in range(0, min(self._height, layout["canvas_rows"] * 2), 2):
            row: list[Cell] = []
            has_bottom = y + 1 < self._height
            for x in range(self._width):
                top = self._preview_pixels.get((x, y), self._render_color(x, y))
                if has_bottom:
                    bottom = self._preview_pixels.get((x, y + 1), self._render_color(x, y + 1))
                else:
                    bottom = self._background
                row.append((top, bottom))
            rows.append(row)
        return rows

    def _cursor_cell(self) -> tuple[int, int] | None:
        """The display cell the cursor is on: (display row, column)."""
        if not self._pixels:
            return None
        return self._cursor_y // 2, self._cursor_x

    def _styled(self, text: str, style: str) -> str:
        """Render ``text`` to truecolor ANSI using rich (no trailing newline)."""
        rendered = Text(text, style=style)
        return "".join(
            self._console.get_style(seg.style).render(seg.text, color_system="truecolor")
            for line in self._console.render_lines(rendered, pad=False)
            for seg in line
            if seg.text
        )

    def _shape_toolbar_geometry(
        self, layout: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """The five shape-tool buttons, right-aligned on the top (header) row."""
        layout = layout or self._compute_layout()
        total = -1  # every button is followed by a 1-column gap; drop the last
        for _ident, label, icon, _action in _SHAPE_SPEC:
            text = icon if self._icons else label
            total += len(text) + 1
        row = layout["top_pad"] + 1  # shares the header row
        col = max(1, layout["term_w"] - total)
        buttons: list[ToolButton] = []
        for ident, label, icon, action in _SHAPE_SPEC:
            text = icon if self._icons else label
            buttons.append(ToolButton(ident, text, action, row, col))
            col += len(text) + 1
        return {"row": row, "width": total, "buttons": buttons}

    def _render_top_line(self) -> str:
        """Header status text + right-aligned shape-tool buttons on one row.

        The header truncates early enough that the buttons never overlap it,
        and the whole row is diffed as one unit in ``_draw_body``/``_redraw_chrome``.
        """
        tool = _TOOL_LABELS[self._tool]
        size_label = (
            f"text size {self._text_scale}"
            if self._tool == TOOL_TEXT
            else f"brush {self._brush_size}"
        )
        text = (
            f"CorvusPixel  {self._width}x{self._height}  "
            f"{'● connected' if self._connected else '○ reconnecting…'}   "
            f"cursor ({self._cursor_x},{self._cursor_y})   "
            f"{size_label} · {tool}   {hex_str(self._color)}"
        )
        if self._palette_mode:
            text += "   palette: arrows move · enter select"
        if self._custom_color_mode:
            fill = "·" * (6 - len(self._hex_buffer))
            text += f"   custom: #{self._hex_buffer}{fill}  (0-9a-f · enter ok · esc off)"
        shape = self._shape_toolbar_geometry()
        avail = max(8, self._layout()["term_w"] - shape["width"] - 2)
        if len(text) > avail:
            text = text[: max(0, avail - 1)] + "…"  # truncate, never wrap
        parts = [self._styled(text, "bold #7fd4ff")]
        term_w = self._layout()["term_w"]
        for button in shape["buttons"]:
            if button.col > term_w:
                continue  # entirely off-screen: never wrap
            label = button.label
            if button.col + len(label) - 1 > term_w:
                label = label[: max(0, term_w - button.col + 1)]
            if button.ident == self._tool:
                label = f"\x1b[7m{label}{RESET}"  # active tool shows reversed
            parts.append(f"\x1b[{button.row};{button.col}H{label}")
        return "".join(parts)

    def _quick_geometry(self, layout: dict[str, int] | None = None) -> dict[str, int]:
        """Row/columns of the always-visible bottom colour row (quick colours +
        one custom-colour swatch). Centered like the palette."""
        layout = layout or self._compute_layout()
        swatch_w = CELL_W * 2
        gap = 2
        total = (len(QUICK_COLORS) + 1) * (swatch_w + gap) - gap  # + 1 = custom
        indent = max(0, (layout["term_w"] - total) // 2)
        return {
            "indent": indent,
            "swatch_w": swatch_w,
            "gap": gap,
            "row": layout["top_pad"] + 5 + layout["canvas_rows"],
        }

    def _render_quick_colors(self, layout: dict[str, int], geom: dict[str, int]) -> None:
        """Draw the quick-colour swatch row; the custom swatch is a rainbow block."""
        file = self._console.file
        self._old_panel = None  # the full-row clear below wipes the shortcuts panel
        file.write(f"\x1b[{geom['row']};1H\x1b[2K")
        for i, (_name, color) in enumerate(QUICK_COLORS):
            left = geom["indent"] + i * (geom["swatch_w"] + geom["gap"])
            file.write(f"\x1b[{geom['row']};{left + 1}H")
            file.write(f"\x1b[38;2;{color[0]};{color[1]};{color[2]}m")
            file.write(f"\x1b[48;2;{color[0]};{color[1]};{color[2]}m")
            if i == self._quick_idx:
                file.write("\x1b[7m")  # selected quick colour shows reversed
            file.write(CELL_CH * (geom["swatch_w"] // CELL_W))
            file.write(RESET)
        # The custom-colour swatch: a 4-column rainbow signalling "more colours".
        i = len(QUICK_COLORS)
        left = geom["indent"] + i * (geom["swatch_w"] + geom["gap"])
        rainbow = [(255, 70, 70), (255, 210, 70), (110, 255, 110), (90, 170, 255)]
        for k, c in enumerate(rainbow):
            file.write(f"\x1b[{geom['row']};{left + 1 + k}H")
            file.write(f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m")
            file.write(f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m")
            file.write(HALF_BLOCK)
            file.write(RESET)

    def _shortcuts_panel(self, avail: int) -> list[list[tuple[str, str]]]:
        """Pack the shortcut groups into panel lines of at most ``avail`` columns.

        Each returned entry is one panel row (drawn top-down on the
        palette-indicator, palette and quick-colour rows): a list of
        ``(header, caption)`` pairs in display order. Groups stay in the order
        they appear in ``_SHORTCUT_GROUPS`` (most useful first); a group too
        wide to fit on a line by itself, or one that would spill onto a fourth
        line, is dropped rather than wrapping or colliding with the swatches.
        Returns ``[]`` when the left margin is too narrow to be legible.
        """
        if avail < 9:
            return []
        lines: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        current_w = 0
        for header, keys in _SHORTCUT_GROUPS:
            caption = "·".join(keys)
            if len(header) + 1 + len(caption) > avail:
                continue  # never fits on a line by itself: drop it
            add = len(header) + 1 + len(caption) + (2 if current else 0)
            if current and current_w + add > avail:
                lines.append(current)
                current, current_w = [], 0
                if len(lines) == 3:
                    break  # the panel has exactly three chrome rows to use
                add -= 2  # a fresh line's first group has no separator
            current.append((header, caption))
            current_w += add
        if current and len(lines) < 3:
            lines.append(current)
        return lines

    def _draw_shortcuts_panel(self, layout: dict[str, int]) -> None:
        """Draw the grouped keyboard-shortcut panel into the bottom-left.

        The panel packs ``_SHORTCUT_GROUPS`` into the left margin of the three
        chrome rows below the canvas, so it never overlaps the centred swatches
        (every line is bounded by the quick-colour row's indent). Only the
        swatch renders clear these rows (and they invalidate ``_old_panel``), so
        the panel redraws exactly when its rows are wiped — a brush-bar drag that
        changes nothing else stays a true no-op.
        """
        geom = self._quick_geometry(layout)
        avail = geom["indent"] - 2  # the margin before the quick-colour swatches
        panel = self._shortcuts_panel(avail)
        key = tuple(tuple(line) for line in panel)
        if not panel:
            self._old_panel = key
            return
        if key == self._old_panel:
            return
        self._old_panel = key
        file = self._console.file
        for i, line in enumerate(panel):
            row = layout["top_pad"] + 3 + layout["canvas_rows"] + i
            text = "  ".join(
                f"{self._styled(header, '#7fd4ff')} {caption}"
                for header, caption in line
            )
            file.write(f"\x1b[{row};1H{text}{RESET}")

    def _palette_geometry(self, layout: dict[str, int] | None = None) -> dict[str, int]:
        """Row/column layout of the visual color palette."""
        layout = layout or self._compute_layout()
        swatch_w = CELL_W * 2
        gap = 2
        total = len(PALETTE) * (swatch_w + gap) - gap
        indent = max(0, (layout["term_w"] - total) // 2)
        return {
            "indent": indent,
            "swatch_w": swatch_w,
            "gap": gap,
            "indicator_row": layout["top_pad"] + 3 + layout["canvas_rows"],
            "row": layout["top_pad"] + 4 + layout["canvas_rows"],
        }

    def _render_palette_rows(self, layout: dict[str, int], geom: dict[str, int]) -> None:
        """Draw the swatch row plus a ``▼`` marker over the selected swatch."""
        file = self._console.file
        self._old_panel = None  # the full-row clears below wipe the shortcuts panel
        file.write(f"\x1b[{geom['indicator_row']};1H\x1b[2K")
        file.write(f"\x1b[{geom['row']};1H\x1b[2K")
        if self._palette_active:
            marker_col = (
                geom["indent"]
                + self._palette_idx * (geom["swatch_w"] + geom["gap"])
                + geom["swatch_w"] // 2
            )
            file.write(f"\x1b[{geom['indicator_row']};{marker_col + 1}H")
            file.write("\x1b[38;2;255;255;255m▼\x1b[0m")
        for i, (name, color) in enumerate(PALETTE):
            left = geom["indent"] + i * (geom["swatch_w"] + geom["gap"])
            file.write(f"\x1b[{geom['row']};{left + 1}H")
            file.write(f"\x1b[38;2;{color[0]};{color[1]};{color[2]}m")
            file.write(f"\x1b[48;2;{color[0]};{color[1]};{color[2]}m")
            if i == self._palette_idx:
                file.write("\x1b[7m")
            file.write(CELL_CH * (geom["swatch_w"] // CELL_W))
            file.write(RESET)

    def _toolbar_geometry(
        self, layout: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """Row/columns of the toolbar: the button list, the brush bar, the row."""
        layout = layout or self._compute_layout()
        total = -1  # every item is followed by a 1-column gap; drop the last one
        for ident, label, icon, _action in _TOOLBAR_SPEC:
            text = icon if self._icons else label
            total += (BRUSH_MAX if ident == "brush_slider" else len(text)) + 1
        indent = max(0, (layout["term_w"] - total) // 2)  # horizontally centered
        row = layout["top_pad"] + 2  # just below the header
        col = indent + 1
        buttons: list[ToolButton] = []
        slider: BrushSlider | None = None
        for ident, label, icon, action in _TOOLBAR_SPEC:
            text = icon if self._icons else label
            if ident == "brush_slider":
                slider = BrushSlider(row, col)
            else:
                buttons.append(ToolButton(ident, text, action, row, col))
            col += (BRUSH_MAX if ident == "brush_slider" else len(text)) + 1
        return {"row": row, "indent": indent, "buttons": buttons, "slider": slider}

    def _render_toolbar(self) -> str:
        """ANSI string for the toolbar row (buttons + the brush-size bar).

        Buttons are clipped to the terminal width, so a narrow window clips
        the far-right buttons instead of wrapping them onto the canvas row.
        """
        geom = self._toolbar_geometry()
        term_w = self._layout()["term_w"]
        parts: list[str] = []
        for button in geom["buttons"]:
            if button.col > term_w:
                continue  # entirely off-screen: never wrap
            label = button.label
            if button.col + len(label) - 1 > term_w:
                label = label[: max(0, term_w - button.col + 1)]
            if button.ident == self._tool:
                label = f"\x1b[7m{label}{RESET}"  # active tool shows reversed
            parts.append(f"\x1b[{button.row};{button.col}H{label}")
        slider = geom["slider"]
        if slider is not None:
            handle = f"\x1b[7m●{RESET}"
            size = self._current_size()  # brush size, or text scale with text active
            dashes_before = "─" * (size - 1)
            dashes_after = "─" * (BRUSH_MAX - size)
            avail = term_w - slider.col + 1
            if avail >= BRUSH_MAX:  # everything fits: normal render
                track = dashes_before + handle + dashes_after
            elif avail > len(dashes_before):  # the handle fits, the tail clips
                track = dashes_before + handle
            else:
                track = dashes_before[:avail]  # only leading dashes
            if track:
                parts.append(f"\x1b[{slider.row};{slider.col}H{track}")
        return "".join(parts)

    def _draw(self) -> None:
        """Repaint only what changed. A layout change forces a full redraw."""
        with self._lock:
            new_layout = self._compute_layout()
            if self._force_full or new_layout != self._layout_info:
                self._force_full = False
                self._layout_info = new_layout
                self._full_redraw()
                return
            self._draw_body()

    def _full_redraw(self) -> None:
        """Clear the whole screen and redraw from scratch (resize / re-centre)."""
        self._console.file.write("\x1b[2J")
        self._old_cells = []
        self._old_header = ""
        self._old_toolbar = ""
        self._old_palette_key = None
        self._old_quick_key = None
        self._old_panel = None
        self._old_cursor_cell = None
        self._cursor_was_reversed = False
        self._old_label_chars = {}
        self._draw_body()

    def _draw_body(self) -> None:
        file = self._console.file
        layout = self._layout()
        rows = self._display_rows()
        self._label_chars = self._build_object_chars()

        header = self._render_top_line()
        if header != self._old_header:
            file.write(f"\x1b[{layout['top_pad'] + 1};1H\x1b[2K")
            file.write(header)
            file.write(RESET)
            self._old_header = header

        toolbar = self._render_toolbar()
        if toolbar != self._old_toolbar:
            file.write(f"\x1b[{layout['top_pad'] + 2};1H\x1b[2K")
            file.write(toolbar)
            file.write(RESET)
            self._old_toolbar = toolbar

        old = self._old_cells
        for y in range(max(len(old), len(rows))):
            new_row = rows[y] if y < len(rows) else None
            old_row = old[y] if y < len(old) else None
            if new_row == old_row:
                continue
            if new_row is None:
                # A row disappeared (canvas shrank): erase it.
                row = layout["top_pad"] + 3 + y
                if row <= layout["term_h"]:
                    file.write(f"\x1b[{row};1H\x1b[2K")
                continue
            if old_row is None or len(new_row) != len(old_row):
                # A brand-new or resized row: erase it and write every cell.
                file.write(f"\x1b[{layout['top_pad'] + 3 + y};1H\x1b[2K")
                for x, cell in enumerate(new_row):
                    self._write_cell(file, y, x, cell)
                    self._overlay_labels(
                        file, rows, y, {x * CELL_W, x * CELL_W + 1}
                    )
            else:
                # Same size as before: rewrite exactly the cells that changed.
                for x, (cell, prev) in enumerate(zip(new_row, old_row)):
                    if cell != prev:
                        self._write_cell(file, y, x, cell)
                        self._overlay_labels(
                            file, rows, y, {x * CELL_W, x * CELL_W + 1}
                        )
        self._old_cells = rows

        # Label overlay diff: a label edit rewrites only the affected terminal
        # columns (restore the pixel cell underneath, then re-overlay the label
        # chars that live there), keeping label changes cheap.
        old_labels = self._old_label_chars
        if old_labels != self._label_chars:
            for pos in set(old_labels) | set(self._label_chars):
                if old_labels.get(pos) != self._label_chars.get(pos):
                    self._redraw_label_region(file, rows, pos[0], {pos[1]})
            self._old_label_chars = self._label_chars

        # Cursor overlay: a blinking reverse-video block at the cursor pixel.
        # When it moves, restore the old cell; when the blink phase flips,
        # redraw the cursor cell so it always tracks with no leftovers.
        self._render_cursor(rows)

        geom = self._palette_geometry(layout)
        palette_key = (
            self._palette_idx,
            self._palette_active,
            geom["indicator_row"],
            geom["row"],
            geom["indent"],
            geom["swatch_w"],
            geom["gap"],
        )
        if palette_key != self._old_palette_key:
            self._old_palette_key = palette_key
            self._render_palette_rows(layout, geom)

        qgeom = self._quick_geometry(layout)
        quick_key = (
            self._quick_idx,
            qgeom["row"],
            qgeom["indent"],
            qgeom["swatch_w"],
            qgeom["gap"],
        )
        if quick_key != self._old_quick_key:
            self._old_quick_key = quick_key
            self._render_quick_colors(layout, qgeom)

        self._draw_shortcuts_panel(layout)

        # Park the terminal cursor below everything so it never wanders.
        park = min(layout["term_h"], layout["top_pad"] + 6 + layout["canvas_rows"])
        file.write(f"\x1b[{park};1H")
        file.flush()

    def _render_cursor(self, rows: list[list[Cell]]) -> None:
        """Draw the blinking cursor over its cell; restore the old cell on move.

        Reverse video alone is invisible when the cell's two pixels share a
        colour (e.g. an empty canvas, where fg == bg), so the cursor is also
        framed by bright ``[ ]`` brackets on the same row whenever the blink is
        "on" — its position stays unambiguous even on a blank canvas.
        """
        file = self._console.file
        new_cursor = self._cursor_cell()
        blink = self._blink_active
        if new_cursor != self._old_cursor_cell:
            if self._old_cursor_cell is not None:
                self._restore_cursor_at(file, rows, *self._old_cursor_cell)
            self._old_cursor_cell = new_cursor
            if new_cursor is not None:
                self._draw_cursor_at(file, rows, *new_cursor, blink)
            self._cursor_was_reversed = blink
        elif blink != self._cursor_was_reversed and new_cursor is not None:
            if blink:
                self._draw_cursor_at(file, rows, *new_cursor, True)
            else:
                self._restore_cursor_at(file, rows, *new_cursor)
            self._cursor_was_reversed = blink

    def _draw_cursor_at(self, file: Any, rows: list[list[Cell]], dy: int, x: int, blink: bool) -> None:
        """Draw the cursor cell (reversed while blinking on) plus its brackets."""
        if dy < len(rows) and x < len(rows[dy]):
            self._write_cell(file, dy, x, rows[dy][x], reverse=blink)
            self._overlay_labels(
                file, rows, dy, {x * CELL_W, x * CELL_W + 1}, reverse=blink
            )
        if not blink:
            return  # brackets only show during the "on" blink phase
        layout = self._layout()
        row = layout["top_pad"] + 3 + dy
        left = layout["left_pad"] + x * CELL_W          # the column before the cell
        right = layout["left_pad"] + (x + 1) * CELL_W + 1  # the column after it
        frame = "\x1b[1;38;2;255;255;255m\x1b[48;2;0;0;0m"  # bright on black: always visible
        if 1 <= left <= layout["term_w"]:
            file.write(f"\x1b[{row};{left}H{frame}[{RESET}")
        if 1 <= right <= layout["term_w"]:
            file.write(f"\x1b[{row};{right}H{frame}]{RESET}")

    def _restore_cursor_at(self, file: Any, rows: list[list[Cell]], dy: int, x: int) -> None:
        """Redraw the cursor cell and its bracket neighbours plain from ``rows``."""
        if dy >= len(rows):
            return
        layout = self._layout()
        row = layout["top_pad"] + 3 + dy
        width = len(rows[dy])
        written: list[int] = []
        if x < width:
            self._write_cell(file, dy, x, rows[dy][x], reverse=False)
            written.append(x)
        if x - 1 >= 0:
            self._write_cell(file, dy, x - 1, rows[dy][x - 1])
            written.append(x - 1)
        else:  # the left bracket was in the padding column: clear it
            col = layout["left_pad"] + x * CELL_W
            if 1 <= col <= layout["term_w"]:
                file.write(f"\x1b[{row};{col}H ")
        if x + 1 < width:
            self._write_cell(file, dy, x + 1, rows[dy][x + 1])
            written.append(x + 1)
        else:  # the right bracket was in the padding column: clear it
            col = layout["left_pad"] + (x + 1) * CELL_W + 1
            if 1 <= col <= layout["term_w"]:
                file.write(f"\x1b[{row};{col}H ")
        # A label char that lives on a restored cell must be re-overlaid so the
        # cursor blink never clobbers it.
        for cx in written:
            self._overlay_labels(file, rows, dy, {cx * CELL_W, cx * CELL_W + 1})

    def _redraw_chrome(self) -> None:
        """Redraw only the non-canvas parts: header, cursor, toolbar, palette,
        status. Used for input that changes nothing on the pixel grid (brush
        size, tool toggles, palette selection), so e.g. dragging the brush bar
        never causes a canvas redraw."""
        with self._lock:
            file = self._console.file
            layout = self._layout()
            rows = self._display_rows()
            self._label_chars = self._build_object_chars()

            header = self._render_top_line()
            if header != self._old_header:
                file.write(f"\x1b[{layout['top_pad'] + 1};1H\x1b[2K")
                file.write(header)
                file.write(RESET)
                self._old_header = header

            self._render_cursor(rows)

            geom = self._palette_geometry(layout)
            palette_key = (
                self._palette_idx,
                self._palette_active,
                geom["indicator_row"],
                geom["row"],
                geom["indent"],
                geom["swatch_w"],
                geom["gap"],
            )
            if palette_key != self._old_palette_key:
                self._old_palette_key = palette_key
                self._render_palette_rows(layout, geom)

            toolbar = self._render_toolbar()
            if toolbar != self._old_toolbar:
                file.write(f"\x1b[{layout['top_pad'] + 2};1H\x1b[2K")
                file.write(toolbar)
                file.write(RESET)
                self._old_toolbar = toolbar

            qgeom = self._quick_geometry(layout)
            quick_key = (
                self._quick_idx,
                qgeom["row"],
                qgeom["indent"],
                qgeom["swatch_w"],
                qgeom["gap"],
            )
            if quick_key != self._old_quick_key:
                self._old_quick_key = quick_key
                self._render_quick_colors(layout, qgeom)

            self._draw_shortcuts_panel(layout)

            # Park the terminal cursor below everything so it never wanders.
            park = min(layout["term_h"], layout["top_pad"] + 6 + layout["canvas_rows"])
            file.write(f"\x1b[{park};1H")
            file.flush()

    def _write_cell(self, file: Any, y: int, x: int, cell: Cell, reverse: bool = False) -> None:
        """Move to a cell and draw it: fg = top pixel, bg = bottom pixel."""
        layout = self._layout()
        row = layout["top_pad"] + 3 + y  # row 1 header, row 2 toolbar
        col = layout["left_pad"] + x * CELL_W + 1
        if row > layout["term_h"] or col + CELL_W - 1 > layout["term_w"]:
            return  # off-screen: clip instead of wrapping / scrolling
        (fr, fg, fb), (br, bg, bb) = cell
        file.write(f"\x1b[{row};{col}H")
        file.write(f"\x1b[38;2;{fr};{fg};{fb}m")
        file.write(f"\x1b[48;2;{br};{bg};{bb}m")
        if reverse:
            file.write("\x1b[7m")  # reverse video marks the cursor cell
        file.write(CELL_CH)
        file.write(RESET)

    def _label_cell(self, col: int, row: int) -> tuple[int, int] | None:
        """Map a terminal (1-based) col/row to a label terminal cell, or None.

        Labels are positioned at whole-terminal-cell resolution, so this does
        NOT floor to display-cell granularity like ``_screen_cell``: the tcol is
        the exact terminal column (0-based within the canvas area).
        """
        layout = self._compute_layout()
        # Display row 0 lives at terminal row top_pad + 3 (header + toolbar).
        trow = row - 1 - (layout["top_pad"] + 2)  # 0-based canvas display row
        tcol = col - 1 - layout["left_pad"]  # 0-based terminal column
        if trow < 0 or trow >= layout["canvas_rows"]:
            return None  # above or below the canvas area
        if tcol < 0 or tcol >= self._width * CELL_W:
            return None
        if layout["left_pad"] + (tcol // CELL_W + 1) * CELL_W > layout["term_w"]:
            return None  # this display cell is clipped off the right edge
        return trow, tcol

    def _overlay_labels(
        self,
        file: Any,
        rows: list[list[Cell]],
        trow: int,
        tcols: set[int],
        reverse: bool = False,
    ) -> None:
        """Overlay the label chars at ``tcols`` of display row ``trow``.

        Each label char renders in the label colour on the pixel cell's bottom
        colour (so it blends into the pixel grid it covers) using the terminal's
        own font — crisp at any size. ``reverse`` (the cursor) swaps fg/bg so a
        label char under the cursor blinks like the block around it.
        """
        if trow < 0 or trow >= len(rows):
            return  # outside the visible canvas area
        layout = self._layout()
        term_row = layout["top_pad"] + 3 + trow
        for tcol in sorted(tcols):
            entry = self._label_chars.get((trow, tcol))
            if entry is None:
                continue
            char, color = entry
            term_col = layout["left_pad"] + tcol + 1
            if term_row > layout["term_h"] or term_col > layout["term_w"]:
                continue  # off-screen: skip (the pixel cell was already written)
            cell_x = tcol // CELL_W
            if 0 <= cell_x < len(rows[trow]):
                br, bg, bb = rows[trow][cell_x][1]  # the cell's bottom pixel
            else:
                br, bg, bb = self._background
            file.write(f"\x1b[{term_row};{term_col}H")
            file.write(f"\x1b[38;2;{color[0]};{color[1]};{color[2]}m")
            file.write(f"\x1b[48;2;{br};{bg};{bb}m")
            if reverse:
                file.write("\x1b[7m")
            file.write(char)
            file.write(RESET)

    def _redraw_label_region(
        self, file: Any, rows: list[list[Cell]], trow: int, tcols: set[int]
    ) -> None:
        """Restore the pixel cells under ``tcols`` and re-overlay their labels.

        The primitive for add/edit/remove of a label char: rewriting the display
        cells (both terminal columns each) brings back the pixel-block content,
        then the overlay re-applies whatever label chars live there — including
        the odd-terminal-column case where a char covers only one of a display
        cell's two columns.
        """
        cell_xs = {tcol // CELL_W for tcol in tcols}
        if 0 <= trow < len(rows):
            for cell_x in cell_xs:
                if 0 <= cell_x < len(rows[trow]):
                    self._write_cell(file, trow, cell_x, rows[trow][cell_x])
        overlay_cols = {
            c for cell_x in cell_xs if 0 <= cell_x
            for c in (cell_x * CELL_W, cell_x * CELL_W + 1)
        }
        self._overlay_labels(file, rows, trow, overlay_cols)

    # -- input handling ---------------------------------------------------------

    def _paint_pixel(self, x: int, y: int, color: Color) -> dict[str, Any] | None:
        """Set one canvas pixel locally; returns its change or None if a no-op."""
        if self._pixels[y][x] == color:
            return None
        self._pixels[y][x] = color
        return {"x": x, "y": y, "color": list(color)}

    def _brush_rect(self, cx: int, cy: int) -> list[tuple[int, int]]:
        """The pixels covered by the square brush centred on (cx, cy)."""
        n = self._brush_size
        half = n // 2
        out: list[tuple[int, int]] = []
        for y in range(cy - half, cy - half + n):
            if not (0 <= y < self._height):
                continue
            for x in range(cx - half, cx - half + n):
                if 0 <= x < self._width:
                    out.append((x, y))
        return out

    def _paint(self, cx: int, cy: int, color: Color) -> list[dict[str, Any]]:
        """Paint the brush square centred on (cx, cy); returns the changes."""
        changes: list[dict[str, Any]] = []
        for x, y in self._brush_rect(cx, cy):
            change = self._paint_pixel(x, y, color)
            if change is not None:
                changes.append(change)
        return changes

    def _brush_color(self) -> Color:
        """What the brush paints right now: eraser paints the background."""
        return self._background if self._tool == TOOL_ERASER else self._color

    def _move_cursor(self, dx: int, dy: int) -> None:
        if self._width:
            self._cursor_x = min(max(self._cursor_x + dx, 0), self._width - 1)
        if self._height:
            self._cursor_y = min(max(self._cursor_y + dy, 0), self._height - 1)

    def _cycle_color(self) -> None:
        self._select_color((self._palette_idx + 1) % len(PALETTE))

    def _select_color(self, index: int) -> None:
        self._palette_idx = index % len(PALETTE)
        self._color = PALETTE[self._palette_idx][1]
        self._palette_active = True
        self._quick_idx = None
        self._pending_chrome = True

    def _select_quick_color(self, index: int) -> None:
        self._quick_idx = index % len(QUICK_COLORS)
        self._color = QUICK_COLORS[self._quick_idx][1]
        self._palette_active = False
        self._pending_chrome = True

    def _paint_at_cursor(self) -> list[dict[str, Any]]:
        """Paint the brush at the cursor with the current color (idempotent).

        Painting a pixel that already has that color is a no-op — space never
        toggles a pixel back to the background; ``x`` is the explicit erase.
        """
        return self._paint(self._cursor_x, self._cursor_y, self._brush_color())

    def _flood_fill(self, x: int, y: int) -> list[dict[str, Any]]:
        """Flood-fill the connected same-colour region at (x, y) with the
        current color, as ONE fill object (a single objects sync, not a pixel
        edit). The fill sees the composite — base + objects — so overlapping
        content is a wall for the fill."""
        if not (0 <= x < self._width and 0 <= y < self._height):
            return []
        grid = [[self._render_color(px, py) for px in range(self._width)]
                for py in range(self._height)]
        if grid[y][x] == self._color:
            return []  # the region is already the fill colour: a no-op
        region = flood_fill_region(grid, x, y)
        if not region:
            return []
        obj = Object(
            oid=self._object_next_id,
            kind="fill",
            color=self._color,
            data={"pixel_count": len(region)},
            pixels=region,
        )
        self._object_next_id += 1
        self._objects.append(obj)
        self._sync_objects()
        self._pending_chrome = False  # the object renders via a full _draw()
        return []

    def _erase(self) -> list[dict[str, Any]]:
        return self._paint(self._cursor_x, self._cursor_y, self._background)

    def _resize_local(self, new_w: int, new_h: int) -> None:
        """Resize the local copy, keeping the top-left region where it fits."""
        old = self._pixels
        old_w, old_h = self._width, self._height
        self._width, self._height = new_w, new_h
        self._pixels = [[self._background] * new_w for _ in range(new_h)]
        for y in range(min(old_h, new_h)):
            for x in range(min(old_w, new_w)):
                self._pixels[y][x] = old[y][x]
        self._clip_objects_to_canvas()
        self._sync_objects()
        self._cursor_x = min(self._cursor_x, max(0, new_w - 1))
        self._cursor_y = min(self._cursor_y, max(0, new_h - 1))

    def _request_resize(self, dw: int, dh: int) -> list[dict[str, Any]]:
        """Grow/shrink the canvas at an edge; the server syncs on the next tick."""
        w = min(max(self._width + dw, MIN_CANVAS), MAX_CANVAS)
        h = min(max(self._height + dh, MIN_CANVAS), MAX_CANVAS)
        if w == self._width and h == self._height:
            return []
        self._resize_local(w, h)
        self._pending_resize = (w, h)
        return []

    # -- toolbar actions (same effect as the matching keyboard shortcut) ----

    def _set_brush_size(self, size: int) -> list[dict[str, Any]]:
        """Clamp and apply a brush size. Only chrome changes — never the canvas."""
        new = max(BRUSH_MIN, min(BRUSH_MAX, size))
        if new == self._brush_size:
            return []  # unchanged: nothing to redraw (throttles slider drags)
        self._brush_size = new
        self._pending_chrome = True
        return []

    def _set_text_scale(self, size: int) -> list[dict[str, Any]]:
        """Clamp and apply the text scale (how big each font pixel renders)."""
        new = max(BRUSH_MIN, min(BRUSH_MAX, size))
        if new == self._text_scale:
            return []
        self._text_scale = new
        self._pending_chrome = True
        return []

    def _current_size(self) -> int:
        """The value the brush slider shows: the text scale while the text tool
        is active, the brush size otherwise. The slider is context-sensitive."""
        return self._text_scale if self._tool == TOOL_TEXT else self._brush_size

    def _set_size(self, size: int) -> list[dict[str, Any]]:
        """Route a slider / +/- change to the size the active tool uses."""
        if self._tool == TOOL_TEXT:
            return self._set_text_scale(size)
        return self._set_brush_size(size)

    def _tool_paint(self) -> list[dict[str, Any]]:
        return self._paint_at_cursor()

    def _set_tool(self, tool: str) -> list[dict[str, Any]]:
        # Switching tools commits any in-progress text entry and any in-progress
        # label entry (finalize is visual-no-change, so a chrome redraw is safe).
        # Leaving the select tool drops the selection — and its border needs a
        # full canvas redraw to erase, so fall back to ``_draw()`` there.
        self._text_finalize()
        self._label_finalize()
        self._tool = tool
        if tool != TOOL_SELECT:
            cleared = self._selected_id is not None
            self._selected_id = None
            self._pending_chrome = not cleared
        else:
            self._pending_chrome = True
        return []

    def _tool_eraser(self) -> list[dict[str, Any]]:
        # 'e' toggles between eraser and paint (like before); picking a shape
        # tool is a separate, one-way selection.
        self._text_finalize()
        self._label_finalize()
        self._tool = TOOL_ERASER if self._tool != TOOL_ERASER else TOOL_PAINT
        if self._selected_id is not None:
            self._selected_id = None
            self._pending_chrome = False  # full redraw clears the border
        else:
            self._pending_chrome = True
        return []

    def _tool_filled_rect(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_FILLED_RECT)

    def _tool_filled_square(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_FILLED_SQUARE)

    def _tool_hollow_rect(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_HOLLOW_RECT)

    def _tool_hollow_square(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_HOLLOW_SQUARE)

    def _tool_line(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_LINE)

    def _tool_fill(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_FILL)

    def _tool_text(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_TEXT)

    def _tool_label(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_LABEL)

    # -- shape drag state machine ---------------------------------------------

    def _shape_cells(
        self, start: tuple[int, int], end: tuple[int, int]
    ) -> set[tuple[int, int]]:
        """The unclipped cells the current shape tool draws from ``start`` to ``end``."""
        x1, y1 = start
        x2, y2 = end
        if self._tool in (TOOL_FILLED_SQUARE, TOOL_HOLLOW_SQUARE):
            x2, y2 = square_end(start, end)
        if self._tool in (TOOL_FILLED_RECT, TOOL_FILLED_SQUARE):
            return fill_rect_cells(x1, y1, x2, y2)
        if self._tool in (TOOL_HOLLOW_RECT, TOOL_HOLLOW_SQUARE):
            return hollow_rect_cells(x1, y1, x2, y2, self._brush_size)
        return thick_line_cells(x1, y1, x2, y2, self._brush_size)

    def _shape_motion(self, x: int, y: int) -> list[dict[str, Any]]:
        """Press starts a shape drag; motion moves its end. Nothing is committed."""
        if self._shape_drag is None:
            self._shape_drag = (x, y)
        self._shape_end = (x, y)
        self._refresh_preview()
        return []

    def _refresh_preview(self) -> None:
        """Recompute the dimmed local preview overlay for the current drag."""
        if self._shape_drag is None or self._shape_end is None:
            self._preview_pixels = {}
            return
        cells = self._shape_cells(self._shape_drag, self._shape_end)
        dimmed = tuple(int(c * PREVIEW_DIM) for c in self._color)
        self._preview_pixels = {cell: dimmed for cell in cells}

    def _commit_shape(self) -> list[dict[str, Any]]:
        """Commit the in-progress shape as one shape object; clears the preview.

        The shape becomes an Object in z-order on top of the base raster, and
        is synced as an objects message (not per-pixel edits).
        """
        if self._shape_drag is None:
            return []
        x1, y1 = self._shape_drag
        x2, y2 = self._shape_end
        if self._tool in (TOOL_FILLED_SQUARE, TOOL_HOLLOW_SQUARE):
            x2, y2 = square_end(self._shape_drag, self._shape_end)
        cells = self._shape_cells((x1, y1), (x2, y2))
        clipped = {(x, y) for (x, y) in cells
                   if 0 <= x < self._width and 0 <= y < self._height}
        self._shape_drag = None
        self._shape_end = None
        self._preview_pixels = {}
        if not clipped:
            return []  # the whole shape was off-canvas: drop it
        shape_type = {
            TOOL_FILLED_RECT: "filled_rect",
            TOOL_FILLED_SQUARE: "filled_square",
            TOOL_HOLLOW_RECT: "hollow_rect",
            TOOL_HOLLOW_SQUARE: "hollow_square",
            TOOL_LINE: "line",
        }[self._tool]
        obj = Object(
            oid=self._object_next_id,
            kind="shape",
            color=self._color,
            data={
                "shape_type": shape_type,
                "x1": min(x1, x2), "y1": min(y1, y2),
                "x2": max(x1, x2), "y2": max(y1, y2),
                "thickness": self._brush_size,
            },
            pixels=clipped,
        )
        self._object_next_id += 1
        self._objects.append(obj)
        self._sync_objects()
        self._pending_chrome = False  # the object renders via a full _draw()
        return []

    def _cancel_shape_drag(self) -> None:
        """Escape during a drag: drop the preview, leave the canvas untouched."""
        if self._shape_drag is None:
            return
        self._shape_drag = None
        self._shape_end = None
        self._preview_pixels = {}
        self._pending_chrome = False  # force a full canvas redraw (not just chrome)
        self._draw()

    # -- text tool state machine -------------------------------------------
    #
    # Selecting the text tool and clicking the canvas starts a text session at
    # that pixel. Each character typed is drawn immediately (into the session's
    # pixel set, composited live — the base raster is never touched), backspace
    # erases the last character, Enter starts a new line below, and Escape drops
    # the whole session. Clicking elsewhere or switching tools finalizes the
    # session: it becomes ONE text Object (glyphs, cursor, pixels) synced to the
    # server. The brush slider and ``+``/``-`` keys set the text scale while the
    # text tool is active; each glyph is drawn at the scale in effect when it is
    # typed, so a mid-session scale change only affects characters after it.

    def _text_place(self, x: int, y: int) -> list[dict[str, Any]]:
        """Start a text session at (x, y); a previous session is finalized."""
        self._text_finalize()
        self._text_active = True
        self._text_x = x
        self._text_y = y
        self._text_line_start_x = x
        self._text_history = []
        self._text_session_pixels = set()
        self._text_color = self._color  # fixed at placement: one colour per text
        self._text_edit_oid = None
        self._text_edit_original = None
        self._cursor_x = x
        self._cursor_y = y
        self._pending_chrome = False  # the session renders via a full _draw()
        return []

    def _text_type_char(self, ch: str) -> list[dict[str, Any]]:
        """Route one keystroke while a text session is active."""
        if ch in ("\r", "\n"):
            return self._text_newline()
        if ch in ("\x7f", "\x08"):  # backspace
            return self._text_backspace()
        if ch == "\t":
            return []  # tab does nothing mid-text
        if ch == " ":
            return self._text_draw_space()
        if ch not in _FONT5X7:
            return []  # unsupported character: ignored
        return self._text_draw_glyph(ch)

    def _text_draw_glyph(self, ch: str) -> list[dict[str, Any]]:
        """Draw one glyph at the insertion point (scaled) and advance right.

        Each font pixel renders as an ``_text_scale`` x ``_text_scale`` block,
        so a glyph box is ``FONT_W * scale`` wide and ``FONT_H * scale`` tall.
        If that box would not fit on the canvas the character is simply not
        accepted (the user can press Enter to drop a line). The scale is read
        at draw time, so changing it mid-session only affects characters typed
        after the change. The glyph's pixels accumulate in
        ``_text_session_pixels`` and are composited live; nothing is committed
        to the base raster until the session finalizes.
        """
        scale = self._text_scale
        if (
            self._text_x + FONT_W * scale > self._width
            or self._text_y + FONT_H * scale > self._height
        ):
            return []  # out of bounds: stop accepting characters
        glyph_drawn: set[tuple[int, int]] = set()
        for px, py in glyph_pixels(ch, self._text_x, self._text_y, scale):
            if 0 <= px < self._width and 0 <= py < self._height:
                glyph_drawn.add((px, py))
        self._text_session_pixels |= glyph_drawn
        self._text_history.append(
            {"kind": "glyph", "x": self._text_x, "y": self._text_y,
             "char": ch, "scale": scale, "pixels": glyph_drawn}
        )
        self._text_x += (FONT_W + FONT_SPACING) * scale
        self._sync_text_caret()
        self._pending_chrome = False
        return []

    def _text_draw_space(self) -> list[dict[str, Any]]:
        """A space advances the insertion point without drawing any pixels."""
        self._text_history.append(
            {"kind": "glyph", "x": self._text_x, "y": self._text_y,
             "char": " ", "scale": self._text_scale, "pixels": set()}
        )
        self._text_x += (FONT_W + FONT_SPACING) * self._text_scale
        self._sync_text_caret()
        self._pending_chrome = False
        return []

    def _text_newline(self) -> list[dict[str, Any]]:
        """Commit the current line and move the insertion point below it.

        Line height scales with the current text scale, so multi-line text at a
        large scale gets the room it needs.
        """
        scale = self._text_scale
        new_y = self._text_y + (FONT_H + FONT_LINE_SPACING) * scale
        if new_y + FONT_H * scale > self._height:
            return []  # no room for another line: ignore Enter
        self._text_history.append(
            {"kind": "newline", "x": self._text_x, "y": self._text_y}
        )
        self._text_x = self._text_line_start_x
        self._text_y = new_y
        self._sync_text_caret()
        self._pending_chrome = False
        return []

    def _text_backspace(self) -> list[dict[str, Any]]:
        """Erase the last character (or undo a newline) and step back."""
        if not self._text_history:
            return []
        entry = self._text_history.pop()
        if entry["kind"] == "newline":
            self._text_x, self._text_y = entry["x"], entry["y"]
        else:
            self._text_session_pixels -= entry["pixels"]
            self._text_x, self._text_y = entry["x"], entry["y"]
        self._sync_text_caret()
        self._pending_chrome = False
        return []

    def _text_cancel(self) -> list[dict[str, Any]]:
        """Escape: drop the whole text session (the base raster was never
        touched). An in-place edit of an existing text object restores the
        pre-edit content — Escape reverts, it never deletes."""
        if not self._text_active:
            return []
        if (self._text_edit_oid is not None
                and self._text_edit_original is not None):
            self._replace_object(self._text_edit_original)
        self._text_active = False
        self._text_history = []
        self._text_session_pixels = set()
        self._text_edit_oid = None
        self._text_edit_original = None
        self._sync_objects()
        self._pending_chrome = False  # force _after_edit to redraw the canvas
        return []

    def _text_data(self) -> dict[str, Any]:
        """The text object's metadata: the text string, per-glyph placement and
        the cursor position. Derived from the session history (which backspace
        has pruned), so it always matches the session pixels."""
        rows: list[list[tuple[int, int, str, int]]] = []
        current: list[tuple[int, int, str, int]] = []
        for entry in self._text_history:
            if entry["kind"] == "newline":
                rows.append(current)
                current = []
            else:
                current.append(
                    (entry["x"], entry["y"], entry["char"], entry["scale"])
                )
        rows.append(current)
        return {
            "text": "\n".join("".join(ch for _x, _y, ch, _s in row)
                              for row in rows),
            "glyphs": [
                {"char": ch, "x": x, "y": y, "scale": s}
                for row in rows for (x, y, ch, s) in row
            ],
            "cursor": [self._text_x, self._text_y],
        }

    def _text_finalize(self) -> None:
        """Commit any in-progress text session: build a text Object (or replace
        the one being edited) and sync. Visual-no-change, so no redraw."""
        if self._text_active:
            if self._text_edit_oid is not None:
                if self._text_session_pixels:
                    self._replace_object(Object(
                        oid=self._text_edit_oid,
                        kind="text",
                        color=self._text_color,
                        data=self._text_data(),
                        pixels=self._text_session_pixels,
                    ))
                else:
                    # The edit removed every character: delete the object.
                    self._objects = [o for o in self._objects
                                     if o.oid != self._text_edit_oid]
                    if self._selected_id == self._text_edit_oid:
                        self._selected_id = None
            elif self._text_session_pixels:
                self._objects.append(Object(
                    oid=self._object_next_id,
                    kind="text",
                    color=self._text_color,
                    data=self._text_data(),
                    pixels=self._text_session_pixels,
                ))
                self._object_next_id += 1
            self._sync_objects()
        self._text_active = False
        self._text_history = []
        self._text_session_pixels = set()
        self._text_edit_oid = None
        self._text_edit_original = None

    def _sync_text_caret(self) -> None:
        """Move the canvas cursor to the text insertion point (clamped)."""
        self._cursor_x = min(self._text_x, max(0, self._width - 1))
        self._cursor_y = min(self._text_y, max(0, self._height - 1))

    # -- label tool / select tool state machine -----------------------------
    #
    # The label tool is the literal-text counterpart to the pixel-art text tool.
    # Selecting it and clicking the canvas starts a label session at that
    # terminal cell. Each character typed is stored in ``_label_session_cells``
    # (terminal-cell coordinates, NOT pixels) and rendered immediately on top of
    # the pixel grid in the terminal's own font; backspace removes the last
    # character, Enter starts a new line below, and Escape cancels the whole
    # in-progress label. Clicking elsewhere or switching tools finalizes it —
    # the session becomes ONE ``Object`` of kind ``"label"`` (id, per-line
    # positions, text, colour, hitbox) and is synced to the server, which
    # see_canvas shows. Labels never touch the pixel grid: they are an overlay.
    #
    # The select tool turns every object (labels, shapes, fills, pixel-text)
    # into manipulable things: clicking inside an object's hitbox selects it (a
    # cyan box-drawing border wraps the hitbox), clicking empty space
    # deselects, dragging moves it (a dimmed preview follows the cursor; release
    # commits the new position), Delete or Backspace removes the object
    # entirely, and Enter re-opens text-based objects (labels and pixel-text)
    # for in-place editing — cursor at the end of its text, backspace/typing
    # modify it in place, and Escape reverts to the pre-edit content (it does
    # not delete, as it does for a fresh session, which has nothing to revert
    # to). Only one object is ever selected, and selection never touches the
    # base pixel canvas.

    def _label_place(self, row: int, col: int) -> list[dict[str, Any]]:
        """Start a fresh label session at terminal cell (row, col); finalize any
        prior text/label session and drop any selection. Returns [] without
        setting ``_pending_chrome`` — label typing produces overlay-only changes,
        so ``_after_edit`` must do a full ``_draw()``, not just a chrome redraw."""
        self._text_finalize()
        self._label_finalize()
        self._selected_id = None
        self._label_active = True
        self._label_row = row
        self._label_col = col
        self._label_line_start_col = col
        self._label_color = self._color
        self._label_history = []
        self._label_session_cells = {}
        self._label_edit_oid = None
        self._label_edit_original = None
        self._sync_label_caret()
        self._pending_chrome = False
        return []

    def _label_type_char(self, ch: str) -> list[dict[str, Any]]:
        """Route one keystroke while a label session is active."""
        if ch in ("\r", "\n"):
            return self._label_newline()
        if ch in ("\x7f", "\x08"):  # backspace
            return self._label_backspace()
        if ch == "\t":
            return []  # tab does nothing mid-label
        if ch == " ":
            return self._label_type(" ")
        return self._label_type(ch)

    def _label_type(self, ch: str) -> list[dict[str, Any]]:
        """Write one literal terminal character at the insertion point and
        advance right. Stops at the right edge of the canvas display area."""
        if self._label_col >= self._width * CELL_W:
            return []  # right edge: stop accepting characters
        pos = (self._label_row, self._label_col)
        self._label_session_cells[pos] = (ch, self._label_color)
        self._label_history.append(
            {"kind": "char", "row": self._label_row, "col": self._label_col,
             "char": ch}
        )
        self._label_col += 1
        self._sync_label_caret()
        return []

    def _label_newline(self) -> list[dict[str, Any]]:
        """Move the insertion point to the start of the next line below."""
        layout = self._compute_layout()
        if self._label_row + 1 >= layout["canvas_rows"]:
            return []  # bottom edge: ignore Enter
        self._label_history.append(
            {"kind": "newline", "row": self._label_row, "col": self._label_col}
        )
        self._label_row += 1
        self._label_col = self._label_line_start_col
        self._sync_label_caret()
        return []

    def _label_backspace(self) -> list[dict[str, Any]]:
        """Erase the last character (or undo a newline) and step back.

        Characters typed and newlines pressed during this session are undone
        from ``_label_history``. Once the history is empty (editing a
        pre-existing label), backspace removes the character before the caret
        from the session, and at the start of a non-first line joins it back to
        the line above. The first character of the whole label has nothing to
        remove: backspace is a no-op there. (The select tool's Delete removes
        whole labels instead — the label tool only ever edits the in-progress
        session.)
        """
        if self._label_history:
            entry = self._label_history.pop()
            if entry["kind"] == "newline":
                self._label_row, self._label_col = entry["row"], entry["col"]
            else:
                self._label_session_cells.pop(
                    (entry["row"], entry["col"]), None
                )
                self._label_row, self._label_col = entry["row"], entry["col"]
            self._sync_label_caret()
            return []
        pos = (self._label_row, self._label_col - 1)
        if pos in self._label_session_cells:
            del self._label_session_cells[pos]
            self._label_col -= 1
            self._sync_label_caret()
            return []
        if self._label_row > 0:
            # Caret at the start of a line: merge it back into the line above.
            for (r, c) in list(self._label_session_cells):
                if r == self._label_row:
                    del self._label_session_cells[(r, c)]
            self._label_row -= 1
            cols = [c for (r, c) in self._label_session_cells
                    if r == self._label_row]
            self._label_col = max(cols) + 1 if cols else self._label_line_start_col
            self._label_line_start_col = min(cols) if cols else self._label_line_start_col
            self._sync_label_caret()
        return []

    def _label_cancel(self) -> list[dict[str, Any]]:
        """Escape: discard the in-progress session. For an in-place edit of an
        existing label the object is restored to its pre-edit content (Escape
        reverts — it does not delete); a fresh label session is simply dropped."""
        if not self._label_active:
            return []
        if (self._label_edit_oid is not None
                and self._label_edit_original is not None):
            self._replace_object(self._label_edit_original)
        self._label_active = False
        self._label_session_cells = {}
        self._label_history = []
        self._label_edit_oid = None
        self._label_edit_original = None
        self._sync_objects()
        self._pending_chrome = False  # force _after_edit to redraw the canvas
        return []

    def _label_finalize(self) -> None:
        """Commit any in-progress label session.

        A fresh session becomes a brand-new ``Object``; an in-place edit
        replaces the object with the same id (an edit that deleted every
        character removes the label). Visual-no-change, so no redraw.
        """
        if self._label_active:
            if self._label_edit_oid is not None:
                if self._label_session_cells:
                    self._replace_object(self._object_from_cells(
                        self._label_edit_oid, self._label_session_cells,
                        self._label_color,
                    ))
                else:
                    # The edit removed every character: delete the object.
                    self._objects = [o for o in self._objects
                                     if o.oid != self._label_edit_oid]
                    if self._selected_id == self._label_edit_oid:
                        self._selected_id = None
            elif self._label_session_cells:
                obj = self._object_from_cells(
                    self._object_next_id, self._label_session_cells,
                    self._label_color,
                )
                self._objects.append(obj)
                self._object_next_id += 1
        self._label_active = False
        self._label_session_cells = {}
        self._label_history = []
        self._label_edit_oid = None
        self._label_edit_original = None
        self._sync_objects()

    def _object_from_cells(
        self, oid: int, cells: dict[tuple[int, int], tuple[str, Color]],
        color: Color,
    ) -> Object:
        """Group session cells back into a label Object. Each row of cells
        becomes one line (cells are always contiguous per row while typing)."""
        lines: list[tuple[int, int, str]] = []
        for row in sorted({r for r, _c in cells}):
            cols = sorted(c for r, c in cells if r == row)
            text = "".join(cells[(row, c)][0] for c in cols)
            lines.append((row, cols[0], text))
        return Object(oid, "label", color, {"lines": lines})

    def _replace_object(self, obj: Object) -> None:
        """Swap the stored object with ``obj.oid`` for ``obj``."""
        for i, existing in enumerate(self._objects):
            if existing.oid == obj.oid:
                self._objects[i] = obj
                return
        self._objects.append(obj)  # unknown id (shouldn't happen): just add it

    def _sync_label_caret(self) -> None:
        """Move the canvas cursor to the label insertion point (clamped to the
        display cell containing the insertion column)."""
        self._cursor_x = min(self._label_col // CELL_W, max(0, self._width - 1))
        self._cursor_y = min(self._label_row * 2, max(0, self._height - 1))

    # -- select tool: selection, move, delete, edit --------------------------

    def _tool_select(self) -> list[dict[str, Any]]:
        return self._set_tool(TOOL_SELECT)

    def _selected_object(self) -> Object | None:
        """The currently selected object, or None."""
        for obj in self._objects:
            if obj.oid == self._selected_id:
                return obj
        return None

    def _pixel_hitbox(
        self, pixels: set[tuple[int, int]]
    ) -> tuple[int, int, int, int] | None:
        """The terminal-cell rect a pixel object's bounds occupy: each pixel is
        CELL_W terminal columns wide and half a display row tall."""
        bounds = pixel_bounds(pixels)
        if bounds is None:
            return None
        x0, y0, x1, y1 = bounds
        return (y0 // 2, x0 * CELL_W, y1 // 2, x1 * CELL_W + CELL_W - 1)

    def _object_hitbox(self, obj: Object) -> tuple[int, int, int, int] | None:
        """The terminal-cell hitbox of an object: labels use their text cells,
        pixel objects (shape/fill/text) their pixel bounds."""
        if obj.kind == "label":
            return label_hitbox(obj)
        return self._pixel_hitbox(obj.pixels)

    def _object_at(self, row: int, col: int) -> Object | None:
        """The topmost object whose hitbox contains terminal cell (row, col),
        or None. Later objects are drawn on top, so they win on overlap."""
        for obj in reversed(self._objects):
            hitbox = self._object_hitbox(obj)
            if hitbox is None:
                continue
            r0, c0, r1, c1 = hitbox
            if r0 <= row <= r1 and c0 <= col <= c1:
                return obj
        return None

    def _selection_hitbox(self) -> tuple[int, int, int, int] | None:
        """The hitbox the selection border should wrap right now: the dragged
        object's preview while dragging, the edited object's current content
        while editing, otherwise the selected object's hitbox."""
        if self._label_drag_preview is not None:
            return label_hitbox(self._label_drag_preview)
        if self._object_drag_preview is not None:
            return self._pixel_hitbox(self._object_drag_preview)
        if self._label_edit_oid is not None:
            return hitbox_from_cells(self._label_session_cells)
        if self._text_edit_oid is not None:
            return self._pixel_hitbox(self._text_session_pixels)
        obj = self._selected_object()
        if obj is None:
            return None
        return self._object_hitbox(obj)

    def _deselect_object(self) -> None:
        """Clear the select tool's selection (and any in-progress drag)."""
        self._selected_id = None
        self._drag_id = None
        self._drag_offset = None
        self._object_drag_offset = None
        self._label_drag_preview = None
        self._object_drag_preview = None
        self._preview_pixels = {}

    def _handle_select_press(
        self, button: int, trow: int, tcol: int,
    ) -> list[dict[str, Any]]:
        """A press inside the canvas display area with the select tool active:
        click an object to select it (switching from any previous selection) and
        arm a drag from that press; click empty space to deselect. A simple
        click with no motion just selects."""
        if button != 0:
            return []
        self._text_finalize()
        self._label_finalize()
        obj = self._object_at(trow, tcol)
        if obj is None:
            self._deselect_object()
            self._pending_chrome = False
            return []  # empty canvas area: deselect
        self._selected_id = obj.oid
        hitbox = self._object_hitbox(obj)
        self._drag_id = obj.oid
        if obj.kind == "label":
            self._drag_offset = (trow - hitbox[0], tcol - hitbox[1])
        else:
            x0, y0, _x1, _y1 = pixel_bounds(obj.pixels)
            self._object_drag_offset = (tcol // CELL_W - x0, trow * 2 - y0)
        self._pending_chrome = False
        return []

    def _update_object_drag(self, row: int, col: int) -> list[dict[str, Any]]:
        """Motion during an object drag: move the dimmed preview to follow the
        cursor, clamped so the whole object stays on the canvas."""
        obj = self._selected_object()
        if obj is None:
            return []
        hitbox = self._object_hitbox(obj)
        if hitbox is None:
            return []
        if obj.kind == "label":
            if self._drag_offset is None:
                return []
            dr = (row - self._drag_offset[0]) - hitbox[0]
            dc = (col - self._drag_offset[1]) - hitbox[1]
            layout = self._compute_layout()
            dr = min(max(dr, -hitbox[0]), (layout["canvas_rows"] - 1) - hitbox[2])
            dc = min(max(dc, -hitbox[1]), (self._width * CELL_W - 1) - hitbox[3])
            lines = [(r + dr, c + dc, text)
                     for (r, c, text) in obj.data["lines"]]
            self._label_drag_preview = Object(
                obj.oid, "label", obj.color, {"lines": lines},
            )
            self._object_drag_preview = None
        else:
            if self._object_drag_offset is None:
                return []
            x0, y0, x1, y1 = pixel_bounds(obj.pixels)
            px, py = col // CELL_W, row * 2
            dx = (px - self._object_drag_offset[0]) - x0
            dy = (py - self._object_drag_offset[1]) - y0
            dx = min(max(dx, -x0), (self._width - 1) - x1)
            dy = min(max(dy, -y0), (self._height - 1) - y1)
            self._object_drag_preview = {
                (x + dx, y + dy) for (x, y) in obj.pixels
            }
            dimmed = tuple(int(c * PREVIEW_DIM) for c in obj.color)
            self._preview_pixels = {
                cell: dimmed for cell in self._object_drag_preview
            }
            self._label_drag_preview = None
        self._pending_chrome = False
        return []

    def _commit_object_drag(self) -> list[dict[str, Any]]:
        """Release: commit any in-progress object drag at the preview position."""
        if self._drag_id is None:
            return []
        self._drag_id = None
        self._drag_offset = None
        self._object_drag_offset = None
        self._preview_pixels = {}
        label_preview = self._label_drag_preview
        self._label_drag_preview = None
        if label_preview is not None:
            self._replace_object(label_preview)
            self._sync_objects()
        pixel_preview = self._object_drag_preview
        self._object_drag_preview = None
        if pixel_preview is not None:
            obj = self._selected_object()
            if obj is not None:
                obj.pixels = pixel_preview
                self._sync_objects()
        self._pending_chrome = False
        return []

    def _delete_selected_object(self) -> list[dict[str, Any]]:
        """Delete/Backspace with the select tool active: remove the selected
        object entirely (and sync the removal)."""
        if self._selected_id is None:
            return []
        self._objects = [o for o in self._objects
                         if o.oid != self._selected_id]
        self._selected_id = None
        self._sync_objects()
        self._pending_chrome = False
        return []

    def _edit_selected_object(self) -> list[dict[str, Any]]:
        """Enter with the select tool active: re-open the selected object for
        in-place editing. Only text-based objects (pixel-text and labels) are
        editable; shapes and fills are a no-op."""
        obj = self._selected_object()
        if obj is None:
            return []
        if obj.kind == "text":
            return self._edit_selected_text()
        if obj.kind == "label":
            return self._edit_selected_label()
        return []  # shapes and fills have nothing to edit

    def _edit_selected_text(self) -> list[dict[str, Any]]:
        """Re-open a pixel-text object for in-place editing: the session starts
        from the object's stored glyphs and cursor so backspace/typing modify
        it in place and Escape restores the original."""
        obj = self._selected_object()
        if obj is None:
            return []
        self._text_finalize()
        self._label_finalize()
        self._text_active = True
        self._text_edit_oid = obj.oid
        self._text_edit_original = Object(
            obj.oid, obj.kind, obj.color, dict(obj.data), set(obj.pixels),
        )
        self._text_color = obj.color
        self._text_history = []
        self._text_session_pixels = set(obj.pixels)
        glyphs = sorted(
            obj.data.get("glyphs", []),
            key=lambda g: (g["y"], g["x"]),
        )
        prev_y = None
        for glyph in glyphs:
            if prev_y is not None and glyph["y"] != prev_y:
                # A line break separated these glyphs: re-insert a newline entry
                # at the current insertion point so the text metadata
                # reconstructs multi-line text correctly.
                self._text_history.append({
                    "kind": "newline", "x": self._text_x, "y": self._text_y,
                })
            pixels = set()
            if glyph["char"] != " ":
                for px, py in glyph_pixels(
                    glyph["char"], glyph["x"], glyph["y"], glyph["scale"],
                ):
                    if 0 <= px < self._width and 0 <= py < self._height:
                        pixels.add((px, py))
            self._text_history.append({
                "kind": "glyph",
                "x": glyph["x"], "y": glyph["y"],
                "char": glyph["char"], "scale": glyph["scale"],
                "pixels": pixels,
            })
            prev_y = glyph["y"]
        cursor = obj.data.get("cursor")
        if isinstance(cursor, list) and len(cursor) == 2:
            self._text_x, self._text_y = int(cursor[0]), int(cursor[1])
        else:
            # Fall back to just after the last glyph.
            self._text_x, self._text_y = 0, 0
            for glyph in glyphs:
                self._text_x = glyph["x"] + (FONT_W + FONT_SPACING) * glyph["scale"]
                self._text_y = glyph["y"]
        self._text_line_start_x = self._text_x
        self._sync_text_caret()
        self._pending_chrome = False
        return []

    def _edit_selected_label(self) -> list[dict[str, Any]]:
        """Enter with the select tool active: re-open the selected label for
        in-place editing, cursor at the end of its last line. Backspace/typing
        modify it in place; Escape reverts to the pre-edit content; finalizing
        (click elsewhere or switch tools) commits the edit."""
        obj = self._selected_object()
        if obj is None:
            return []
        self._text_finalize()
        self._label_finalize()
        self._label_edit_oid = obj.oid
        self._label_edit_original = Object(
            obj.oid, "label", obj.color, {"lines": list(obj.data["lines"])},
        )
        self._label_active = True
        self._label_color = obj.color
        self._label_session_cells = {
            (row, col): (char, obj.color)
            for (row, col), (char, _color) in label_cells(obj).items()
        }
        last_row, last_col, last_text = obj.data["lines"][-1]
        self._label_row = last_row
        self._label_col = last_col + len(last_text)
        self._label_line_start_col = last_col
        self._label_history = []
        self._sync_label_caret()
        self._pending_chrome = False
        return []

    def _build_object_chars(self) -> dict[tuple[int, int], tuple[str, Color]]:
        """The current label overlay: stored label objects, any in-progress
        session, the dimmed drag preview, and the selection border ring.

        The stored loop skips the label being edited (it renders via the
        session cells) and the one being dragged (it renders via the dimmed
        preview), so their previous cells never linger as stale overlay pixels.
        """
        out: dict[tuple[int, int], tuple[str, Color]] = {}
        for obj in self._objects:
            if obj.kind != "label":
                continue
            if (self._label_drag_preview is not None
                    and obj.oid == self._label_drag_preview.oid):
                continue
            if (self._label_edit_oid is not None
                    and obj.oid == self._label_edit_oid):
                continue
            for (row, col), (char, _color) in label_cells(obj).items():
                out[(row, col)] = (char, obj.color)
        for (row, col), (char, _color) in self._label_session_cells.items():
            out[(row, col)] = (char, self._label_color)
        if self._label_drag_preview is not None:
            dimmed = tuple(int(c * PREVIEW_DIM)
                           for c in self._label_drag_preview.color)
            for (row, col), (char, _color) in label_cells(
                self._label_drag_preview,
            ).items():
                out[(row, col)] = (char, dimmed)
        hitbox = self._selection_hitbox()
        if hitbox is not None:
            for (row, col), char in label_border_cells(hitbox).items():
                out[(row, col)] = (char, SELECTION_COLOR)
        return out

    def _object_wire(self) -> list[dict[str, Any]]:
        """The stored objects as the wire/see_canvas representation."""
        return [
            {
                "id": obj.oid,
                "kind": obj.kind,
                "color": list(obj.color),
                "data": obj.data,
                "pixels": [list(p) for p in sorted(obj.pixels)],
            }
            for obj in self._objects
        ]

    def _sync_objects(self) -> None:
        """Push the stored objects to the server whenever they changed."""
        wire = self._object_wire()
        if wire != self._last_objects_wire:
            self._last_objects_wire = wire
            self._send_objects()

    def _send_objects(self) -> None:
        """Broadcast the full object list to the server."""
        if self._sock is None:
            return
        payload = json.dumps({
            "type": "objects",
            "objects": self._last_objects_wire or self._object_wire(),
        }) + "\n"
        try:
            self._sock.sendall(payload.encode())
        except OSError:
            pass

    def _apply_objects(self, objects: list[Any]) -> None:
        """Replace stored objects from a server ``objects`` message. Any
        in-progress text/label session and drag is cancelled (it was not
        synced)."""
        self._text_active = False
        self._text_history = []
        self._text_session_pixels = set()
        self._text_edit_oid = None
        self._text_edit_original = None
        self._label_active = False
        self._label_session_cells = {}
        self._label_history = []
        self._label_edit_oid = None
        self._label_edit_original = None
        self._drag_id = None
        self._drag_offset = None
        self._label_drag_preview = None
        self._object_drag_offset = None
        self._object_drag_preview = None
        parsed: list[Object] = []
        for entry in objects:
            obj = self._parse_object(entry)
            if obj is not None:
                parsed.append(obj)
        self._objects = parsed
        if parsed:
            self._object_next_id = max(self._object_next_id,
                                       max(o.oid for o in parsed) + 1)
        if (self._selected_id is not None and not any(
            o.oid == self._selected_id for o in parsed
        )):
            self._selected_id = None
        self._last_objects_wire = self._object_wire()

    def _parse_object(self, entry: Any) -> Object | None:
        """Validate one wire object dict into an Object, or None if malformed."""
        if not (isinstance(entry, dict)
                and "id" in entry and "kind" in entry
                and "color" in entry and "data" in entry):
            return None
        oid = entry["id"]
        kind = entry["kind"]
        if not isinstance(oid, int) or kind not in (
            "shape", "fill", "text", "label",
        ):
            return None
        try:
            rgb = tuple(int(c) for c in entry["color"])
        except (TypeError, ValueError):
            return None
        if len(rgb) != 3:
            return None
        data = entry["data"]
        if not isinstance(data, dict):
            return None
        pixels: set[tuple[int, int]] = set()
        if kind != "label":
            for cell in entry.get("pixels", []):
                if not (isinstance(cell, (list, tuple)) and len(cell) == 2):
                    continue
                try:
                    x, y = int(cell[0]), int(cell[1])
                except (TypeError, ValueError):
                    continue
                pixels.add((x, y))
        return Object(oid, kind, Color(rgb), data, pixels)

    def _tool_brush_dec(self) -> list[dict[str, Any]]:
        return self._set_size(self._current_size() - 1)

    def _tool_brush_inc(self) -> list[dict[str, Any]]:
        return self._set_size(self._current_size() + 1)

    def _tool_palette(self) -> list[dict[str, Any]]:
        self._palette_mode = True
        self._pending_chrome = True
        return []

    def _tool_col_dec(self) -> list[dict[str, Any]]:
        return self._request_resize(-1, 0)

    def _tool_col_inc(self) -> list[dict[str, Any]]:
        return self._request_resize(1, 0)

    def _tool_row_dec(self) -> list[dict[str, Any]]:
        return self._request_resize(0, -1)

    def _tool_row_inc(self) -> list[dict[str, Any]]:
        return self._request_resize(0, 1)

    def _tool_quit(self) -> list[dict[str, Any]]:
        self._quit.set()
        return []

    def _screen_cell(self, col: int, row: int) -> tuple[int, int] | None:
        """Map a terminal (1-based) col/row to a canvas cell, or None."""
        layout = self._compute_layout()
        rel = row - 1 - layout["top_pad"]  # 0-based; 0 = header, 1 = toolbar
        if rel < 2:
            return None
        dy = rel - 2
        if dy < 0 or dy >= layout["canvas_rows"]:
            return None  # below the canvas (palette / status rows)
        x = (col - 1 - layout["left_pad"]) // CELL_W
        if x < 0 or x >= self._width:
            return None
        if layout["left_pad"] + (x + 1) * CELL_W > layout["term_w"]:
            return None  # this cell is clipped (its right edge is off-screen)
        return x, dy

    def _palette_hit(self, col: int, row: int) -> int | None:
        """The palette index a click landed on, or None."""
        geom = self._palette_geometry()
        if row != geom["row"]:
            return None
        idx = (col - 1 - geom["indent"]) // (geom["swatch_w"] + geom["gap"])
        if 0 <= idx < len(PALETTE):
            return idx
        return None

    def _handle_mouse(
        self, button: int, col: int, row: int, pressed: bool
    ) -> list[dict[str, Any]]:
        """SGR mouse event: drawing-tool button, toolbar button / brush bar,
        palette swatch, quick-colour swatch, or paint — or a shape drag.

        With Paint/Eraser active, a click (or click-drag) on the canvas fills
        both halves of the cell under the cursor with the brush, exactly as
        before. The bucket fill paints the connected same-colour region in one
        click; the text tool places (or moves) its insertion point. With a shape
        tool active, press starts a drag, motion moves the preview's end point,
        and release commits the shape as a normal edit; a release with no drag
        in progress is a no-op. With the select tool active, a click on a label
        selects it (and starts a move-drag), a click on empty canvas deselects,
        and motion/release drive the move's dimmed preview and commit. Toolbar
        and swatch clicks act like the matching keyboard shortcut (and leave
        palette mode, mirroring keyboard behaviour). Any click also cancels an
        in-progress custom-colour hex input, and every non-text press finalizes
        any in-progress text entry.
        """
        if not pressed:
            if self._tool == TOOL_SELECT:
                return self._commit_object_drag()  # release: commit the move
            return self._commit_shape()  # release: commit any in-progress shape
        if self._custom_color_mode:
            self._exit_custom_color()
        if button not in (0, 4, 32, 36):
            return []  # middle/right button, or a stray event
        # With the select tool, every press inside the canvas display area is a
        # selection action (select / deselect / start a move-drag); motion keeps
        # a move-drag's preview moving. Presses on the chrome rows fall through
        # to the normal toolbar / palette / quick-colour hit-testing below.
        if self._tool == TOOL_SELECT:
            label_hit = self._label_cell(col, row)
            if button in (32, 36):  # mouse motion during a drag
                if self._drag_id is not None and label_hit is not None:
                    return self._update_object_drag(*label_hit)
                return []
            if label_hit is not None:
                return self._handle_select_press(button, *label_hit)
        # A canvas click with the text tool places the insertion point. That is
        # the one press that does not finalize text here (any old session is
        # finalized-and-restarted inside _text_place).
        hit = self._screen_cell(col, row)
        if hit is not None and self._tool == TOOL_TEXT:
            cell_col, display_row = hit
            if button == 0:
                return self._text_place(cell_col, display_row * 2)
            return []  # drag/wheel with the text tool: nothing to do
        # A canvas click with the label tool places the label caret at
        # whole-terminal-cell resolution (not display-cell granularity). A
        # click on a cell that is clipped off the right edge of the window
        # paints nothing.
        label_hit = self._label_cell(col, row)
        if self._tool == TOOL_LABEL and label_hit is not None:
            if button == 0:
                return self._label_place(*label_hit)
            return []  # drag/wheel with the label tool: nothing to do
        if self._tool == TOOL_LABEL:
            # On the canvas but clipped (or below it): the label tool paints
            # nothing; don't fall through to the paint path.
            if self._screen_cell(col, row) is not None:
                return []
        # The brush slider is a live size control: with the text tool active it
        # sets the text scale without finalizing the in-progress text, so a
        # mid-session scale change only affects characters typed after it.
        geom = self._toolbar_geometry()
        if row == geom["row"]:
            slider = geom["slider"]
            if slider is not None and slider.contains(col, row):
                if self._palette_mode:
                    self._palette_mode = False
                    self._pending_chrome = True
                return self._set_size(slider.brush_size_for(col))
        # Every other press finalizes any in-progress text or label entry.
        self._text_finalize()
        self._label_finalize()
        shape = self._shape_toolbar_geometry()
        if row == shape["row"]:
            for b in shape["buttons"]:
                if b.contains(col, row):
                    if self._palette_mode:
                        self._palette_mode = False
                        self._pending_chrome = True
                    return getattr(self, b.action)()
        if row == geom["row"]:
            for b in geom["buttons"]:
                if b.contains(col, row):
                    if self._palette_mode:
                        self._palette_mode = False
                        self._pending_chrome = True
                    return getattr(self, b.action)()
            return []  # toolbar spacing: nothing to do
        idx = self._palette_hit(col, row)
        if idx is not None:
            if self._palette_mode:
                self._palette_mode = False
            self._select_color(idx)
            return []
        quick = self._quick_hit(col, row)
        if quick is not None:
            if self._palette_mode:
                self._palette_mode = False
            if quick == "custom":
                self._open_custom_color()
            else:
                self._select_quick_color(quick)
            self._pending_chrome = True
            return []
        hit = self._screen_cell(col, row)
        if hit is None:
            return []
        cell_col, display_row = hit
        top_y = display_row * 2
        if self._tool == TOOL_FILL:
            return self._flood_fill(cell_col, top_y) if button == 0 else []
        if self._tool in SHAPE_TOOLS:
            return self._shape_motion(cell_col, top_y)
        color = self._brush_color()
        changes = self._paint(cell_col, top_y, color)
        bottom_y = top_y + 1
        if bottom_y < self._height:
            change = self._paint_pixel(cell_col, bottom_y, color)
            if change is not None:
                changes.append(change)
        return changes

    def _quick_hit(self, col: int, row: int) -> int | str | None:
        """The quick-colour a click landed on, ``"custom"`` for the rainbow
        swatch, or None (not on the quick-colour row)."""
        geom = self._quick_geometry()
        if row != geom["row"]:
            return None
        idx = (col - 1 - geom["indent"]) // (geom["swatch_w"] + geom["gap"])
        if 0 <= idx < len(QUICK_COLORS):
            return idx
        if idx == len(QUICK_COLORS):
            return "custom"
        return None

    def _open_custom_color(self) -> None:
        """Enter hex-input mode (typing ``#rrggbb``, Enter picks, Esc cancels)."""
        self._custom_color_mode = True
        self._hex_buffer = ""
        self._pending_chrome = True

    def _exit_custom_color(self) -> None:
        self._custom_color_mode = False
        self._hex_buffer = ""
        self._pending_chrome = True

    def _handle_char(self, ch: str) -> list[dict[str, Any]]:
        """Handle a plain (non-escape) key. Returns the pixel changes, if any."""
        if self._custom_color_mode:
            if ch in "0123456789abcdefABCDEF" and len(self._hex_buffer) < 6:
                self._hex_buffer += ch.lower()
                self._pending_chrome = True
            elif ch in ("\x7f", "\x08"):  # backspace
                self._hex_buffer = self._hex_buffer[:-1]
                self._pending_chrome = True
            elif ch in ("\r", "\n"):
                if len(self._hex_buffer) == 6:
                    self._color = parse_hex("#" + self._hex_buffer)
                    self._exit_custom_color()
            elif ch == "\t":
                self._exit_custom_color()
            return []  # hex mode swallows every other key
        if self._text_active:
            # With the text tool active, +/- set the text scale instead of
            # drawing (or being ignored): the session keeps running, so the
            # change only affects characters typed after it.
            if ch in "+=":
                return self._set_size(self._current_size() + 1)
            if ch in "-_":
                return self._set_size(self._current_size() - 1)
            return self._text_type_char(ch)  # an active text entry swallows keys
        if self._label_active:
            # An active label swallows every key (including +/-) as a literal
            # character — the label renders real terminal text, not brush art.
            return self._label_type_char(ch)
        if self._tool == TOOL_SELECT:
            # Delete/Backspace removes the selected object; Enter re-opens it
            # for in-place editing (text-based objects only). Every other key
            # falls through to normal handling, so tool switches, painting and
            # quit keep working.
            if ch in ("\x7f", "\x08") and self._selected_id is not None:
                return self._delete_selected_object()
            if ch in ("\r", "\n") and self._selected_id is not None:
                return self._edit_selected_object()
        if ch == "q":
            self._quit.set()
            return []
        if ch == "\t":
            self._palette_mode = not self._palette_mode
            self._pending_chrome = True
            return []
        if self._palette_mode:
            if ch in ("\r", "\n", " "):
                self._palette_mode = False  # confirm the highlighted swatch
                self._pending_chrome = True
                return []
            self._palette_mode = False  # any other key leaves palette mode
            self._pending_chrome = True
        if ch == " ":
            if self._tool == TOOL_FILL:
                return self._flood_fill(self._cursor_x, self._cursor_y)
            return self._paint_at_cursor()
        if ch == "x":
            return self._erase()
        if ch == "e":
            return self._tool_eraser()
        if ch == "p":
            return self._set_tool(TOOL_PAINT)
        if ch == "r":
            return self._set_tool(TOOL_FILLED_RECT)
        if ch == "o":
            return self._set_tool(TOOL_HOLLOW_RECT)
        if ch == "f":
            return self._set_tool(TOOL_FILLED_SQUARE)
        if ch == "s":
            return self._set_tool(TOOL_HOLLOW_SQUARE)
        if ch == "l":
            return self._set_tool(TOOL_LINE)
        if ch == "b":
            return self._set_tool(TOOL_FILL)
        if ch == "t":
            return self._set_tool(TOOL_TEXT)
        if ch == "a":
            return self._set_tool(TOOL_LABEL)
        if ch == "v":
            return self._set_tool(TOOL_SELECT)
        if ch in "+=":
            return self._set_size(self._current_size() + 1)
        if ch in "-_":
            return self._set_size(self._current_size() - 1)
        if ch == "[":
            return self._request_resize(-1, 0)
        if ch == "]":
            return self._request_resize(1, 0)
        if ch == "{":
            return self._request_resize(0, -1)
        if ch == "}":
            return self._request_resize(0, 1)
        if ch == "c":
            self._cycle_color()
            return []
        if ch in "12345678":
            self._select_color(int(ch) - 1)
            return []
        return []

    def _handle_csi(self, params: str, final: str) -> list[dict[str, Any]]:
        """Handle one CSI sequence. Returns the pixel changes, if any."""
        if final in ("A", "B", "C", "D"):
            if self._custom_color_mode:
                return []  # arrows do nothing mid hex input
            if self._text_active:
                return []  # the text caret is driven by typing, not arrows
            if self._label_active:
                return []  # the label caret is driven by typing, not arrows
            dx, dy = {"A": (0, -1), "B": (0, 1), "C": (1, 0), "D": (-1, 0)}[final]
            if self._palette_mode:
                self._select_color(self._palette_idx + dx + dy * 4)
                return []
            self._move_cursor(dx, dy)
            return []
        if final in ("M", "m") and params.startswith("<"):
            # SGR mouse: <button;col;row>
            try:
                button, col, row = (int(p) for p in params[1:].split(";"))
            except ValueError:
                return []
            return self._handle_mouse(button, col, row, pressed=(final == "M"))
        return []

    # -- input read loop ---------------------------------------------------------

    def _read_byte(self) -> bytes | None:
        data = self._input_stream.read(1)
        if data in ("", None):
            return None
        if isinstance(data, str):
            return data.encode("utf-8")
        return data

    def _read_escape_sequence(self) -> list[dict[str, Any]]:
        """Read a full ESC sequence after the leading ``\\x1b`` byte."""
        try:
            second = self._read_byte()
        except (OSError, ValueError):
            return []
        if second != b"[":
            if self._custom_color_mode:
                self._exit_custom_color()  # a stray Esc cancels the hex input
            if self._shape_drag is not None:
                self._cancel_shape_drag()  # a stray Esc cancels an in-progress shape
            if self._text_active:
                return self._text_cancel()  # a stray Esc reverts the text session
            if self._label_active:
                return self._label_cancel()  # a stray Esc cancels the label session
            return []  # Alt+key or a stray ESC: ignore
        buf = ""
        while True:
            try:
                byte = self._read_byte()
            except (OSError, ValueError):
                return []
            if byte is None:
                return []
            code = byte[0]
            if 0x40 <= code <= 0x7E:  # final byte
                return self._handle_csi(buf, chr(code))
            buf += chr(code)

    def _input_loop(self) -> None:
        """Read bytes from the terminal and turn them into edits."""
        while not self._quit.is_set():
            try:
                first = self._read_byte()
            except (OSError, ValueError):
                break
            if first is None:
                break
            if first == b"\x03":  # Ctrl+C
                self._quit.set()
                break
            if first == b"\x1b":
                changes = self._read_escape_sequence()
            else:
                try:
                    ch = first.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                changes = self._handle_char(ch)
            self._after_edit(changes)

    def _after_edit(self, changes: list[dict[str, Any]]) -> None:
        """Redraw and sync any edits/resizes to the server."""
        with self._lock:
            chrome = self._pending_chrome
            self._pending_chrome = False
            if chrome and not changes and self._pending_resize is None and not self._force_full:
                # Brush/tool/palette-only input: redraw just the chrome, never
                # the canvas cells (e.g. every drag step of the brush bar).
                self._redraw_chrome()
            else:
                self._draw()
            self._send_edit(changes)
            if self._pending_resize is not None:
                self._send_resize(*self._pending_resize)
                self._pending_resize = None

    def _send_edit(self, changes: list[dict[str, Any]]) -> None:
        if not changes or self._sock is None:
            return
        payload = (json.dumps({"type": "edit", "changes": changes}) + "\n").encode("utf-8")
        try:
            self._sock.sendall(payload)
        except OSError:
            pass

    def _send_resize(self, width: int, height: int) -> None:
        if self._sock is None:
            return
        payload = (
            json.dumps({"type": "resize", "width": width, "height": height}) + "\n"
        ).encode("utf-8")
        try:
            self._sock.sendall(payload)
        except OSError:
            pass

    # -- terminal setup -----------------------------------------------------------

    def _enter_raw_mode(self) -> bool:
        try:
            fd = self._input_stream.fileno()
        except (AttributeError, OSError):
            return False  # not a real terminal (e.g. a test stream)
        try:
            self._old_termios = termios.tcgetattr(fd)
            tty.setraw(fd)
            return True
        except termios.error:
            return False

    def _enable_mouse(self) -> None:
        self._console.file.write("\x1b[?1002h\x1b[?1006h\x1b[?25l")
        self._console.file.flush()

    def _restore_terminal(self, raw: bool) -> None:
        self._console.file.write("\x1b[?1002l\x1b[?1006l\x1b[?25h")
        self._console.file.flush()
        if raw and self._old_termios is not None:
            try:
                termios.tcsetattr(self._input_stream.fileno(), termios.TCSADRAIN, self._old_termios)
            except (OSError, termios.error):
                pass

    # -- connection / main loop ----------------------------------------------------

    def _connect(self) -> socket.socket | None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._socket_path)
            return sock
        except OSError:
            sock.close()
            return None

    def _set_connected(self, connected: bool) -> None:
        if self._connected != connected:
            self._connected = connected
            self._draw()

    def _pump_socket(self, sock: socket.socket) -> None:
        """Read server messages from ``sock`` until it closes or we quit.

        Uses ``recv`` directly rather than ``makefile().readline()``: a file
        object made from a socket whose timeout has fired is poisoned — after
        the first timeout every read raises ``OSError('cannot read from timed
        out object')`` (a plain OSError, not ``socket.timeout``), which the old
        loop read as a disconnect and reconnected every 0.2s — the status bar
        flickering between "connected" and "reconnecting…". ``recv`` raises
        ``socket.timeout`` cleanly on every timeout, so idle time just pumps
        the blink and re-centring.
        """
        buffer = b""
        while not self._quit.is_set():
            try:
                chunk = sock.recv(65536)
            except InterruptedError:
                continue  # a signal (e.g. SIGWINCH) interrupted the read
            except socket.timeout:
                self._pump()  # idle: blink + re-centre
                continue
            except OSError:
                return
            if not chunk:
                return  # the server closed the socket
            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                self._apply(message)
                self._pump()

    def run(self) -> None:
        """Connect (retrying forever), draw the canvas, and pump user input."""
        raw = self._enter_raw_mode()
        self._enable_mouse()
        self._install_winch_handler()
        input_thread = threading.Thread(
            target=self._input_loop, name="corvuspixel-input", daemon=True
        )
        input_thread.start()
        try:
            with Live(
                console=self._console,
                renderable=None,
                auto_refresh=False,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
            ):
                self._draw()
                while not self._quit.is_set():
                    sock = self._connect()
                    if sock is None:
                        self._set_connected(False)
                        time.sleep(0.2)
                        self._pump()
                        continue
                    self._sock = sock
                    self._set_connected(True)
                    try:
                        sock.settimeout(0.2)
                        self._pump_socket(sock)
                    except OSError:
                        pass
                    finally:
                        self._sock = None
                        try:
                            sock.close()
                        except OSError:
                            pass
                    self._set_connected(False)
        except KeyboardInterrupt:
            pass
        finally:
            self._restore_terminal(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET,
        help="Unix socket the MCP server listens on",
    )
    parser.add_argument(
        "--background", default="#101028", help="fallback color for odd-height canvases"
    )
    args = parser.parse_args()
    CanvasApp(args.socket, parse_hex(args.background)).run()


if __name__ == "__main__":
    main()
