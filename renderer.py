"""CorvusPixel renderer — listens on a Unix socket and paints the canvas live.

The MCP server (:mod:`server`) pushes JSON diffs over a Unix domain socket.
This process connects to that socket (retrying forever), applies the diffs, and
repaints only the cells that changed, using the ``▀`` half-block character so
that each terminal row shows two canvas rows:

    foreground color  = top canvas row's pixel
    background color  = the canvas row below it

Requires a truecolor (24-bit) terminal.

Why not stock rich ``Live``? rich's ``Live`` erases and redraws its *entire*
region on every refresh, which flickers. So we use ``Live`` purely as the
context manager for terminal lifecycle (hide/show the cursor, clean exit) and do
our own cell-diff rendering on top: every change is written as an ANSI cursor
move plus a styled ``▀`` for exactly the cells that changed. No full-screen
redraws.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from typing import Any

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

DEFAULT_SOCKET = os.environ.get("CORVUSPIXEL_SOCK", "/tmp/corvuspixel.sock")


def parse_hex(color: str) -> Color:
    """Parse a ``#rrggbb`` color string into an ``(r, g, b)`` tuple."""
    value = color.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"color must be '#rrggbb', got {color!r}")
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        raise ValueError(f"invalid hex color {color!r}") from None


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #


class PixelRenderer:
    """Applies canvas diffs from the socket and draws them to the terminal.

    ``console`` is injectable so tests can capture output with a fake terminal.
    """

    def __init__(
        self,
        socket_path: str,
        background: Color = (0, 0, 0),
        console: Console | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._background = background
        self._width = 0
        self._height = 0
        self._pixels: list[list[Color]] = []
        self._connected = False

        self._console = console or Console(color_system="truecolor")
        # What is currently drawn on screen (row 1 = header, rows 2+ = canvas).
        self._old_cells: list[list[Cell]] = []
        self._old_header = ""

    # -- socket message handling ---------------------------------------------

    def _apply(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "full":
            self._resize(
                message["width"], message["height"], tuple(message.get("background", self._background))
            )
            for y in range(min(self._height, len(message["pixels"]))):
                row = message["pixels"][y]
                for x in range(min(self._width, len(row))):
                    self._pixels[y][x] = tuple(row[x])
        elif msg_type == "update":
            if not self._pixels:
                return
            for change in message["changes"]:
                x, y = change["x"], change["y"]
                if 0 <= x < self._width and 0 <= y < self._height:
                    self._pixels[y][x] = tuple(change["color"])
        else:
            return
        self._draw()

    def _resize(self, width: int, height: int, background: Color) -> None:
        self._width, self._height = width, height
        self._background = background
        self._pixels = [[background] * width for _ in range(height)]

    # -- drawing ---------------------------------------------------------------

    def _display_rows(self) -> list[list[Cell]]:
        """Current desired screen content: one cell per (2 vertical pixels)."""
        rows: list[list[Cell]] = []
        for y in range(0, self._height, 2):
            row: list[Cell] = []
            has_bottom = y + 1 < self._height
            for x in range(self._width):
                top = self._pixels[y][x]
                bottom = self._pixels[y + 1][x] if has_bottom else self._background
                row.append((top, bottom))
            rows.append(row)
        return rows

    def _render_header(self) -> str:
        status = (
            f"CorvusPixel  {self._width}x{self._height}  "
            f"{'● connected' if self._connected else '○ reconnecting…'}"
        )
        text = Text(status, style="bold #7fd4ff")
        return "".join(
            self._console.get_style(seg.style).render(seg.text, color_system="truecolor")
            for line in self._console.render_lines(text, pad=False)
            for seg in line
            if seg.text
        )

    def _draw(self) -> None:
        """Repaint only the header/cells that actually changed on screen."""
        file = self._console.file
        rows = self._display_rows()

        header = self._render_header()
        if header != self._old_header:
            file.write("\x1b[1;1H\x1b[2K")
            file.write(header)
            file.write(RESET)
            self._old_header = header

        old = self._old_cells
        for y in range(max(len(old), len(rows))):
            new_row = rows[y] if y < len(rows) else None
            old_row = old[y] if y < len(old) else None
            if new_row == old_row:
                continue
            if new_row is None:
                # A row disappeared (canvas shrank): erase it.
                file.write(f"\x1b[{y + 2};1H\x1b[2K")
                continue
            if old_row is None or len(new_row) != len(old_row):
                # A brand-new or resized row: erase it and write every cell.
                file.write(f"\x1b[{y + 2};1H\x1b[2K")
                for x, cell in enumerate(new_row):
                    self._write_cell(file, y, x, cell)
            else:
                # Same size as before: rewrite exactly the cells that changed.
                for x, (cell, prev) in enumerate(zip(new_row, old_row)):
                    if cell != prev:
                        self._write_cell(file, y, x, cell)
        self._old_cells = rows

        # Park the cursor on the line below the canvas so it never wanders.
        file.write(f"\x1b[{len(rows) + 2};1H")
        file.flush()

    @staticmethod
    def _write_cell(file: Any, y: int, x: int, cell: Cell) -> None:
        """Move to a cell and draw it: fg = top pixel, bg = bottom pixel."""
        (fr, fg, fb), (br, bg, bb) = cell
        file.write(f"\x1b[{y + 2};{x + 1}H")  # row 1 is the header
        file.write(f"\x1b[38;2;{fr};{fg};{fb}m")
        file.write(f"\x1b[48;2;{br};{bg};{bb}m")
        file.write(HALF_BLOCK)
        file.write(RESET)

    # -- connection / main loop ------------------------------------------------

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

    def run(self) -> None:
        """Connect (retrying forever) and draw every message until interrupted."""
        with Live(
            console=self._console,
            renderable=None,
            auto_refresh=False,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        ):
            self._draw()
            try:
                while True:
                    sock = self._connect()
                    if sock is None:
                        self._set_connected(False)
                        time.sleep(0.5)
                        continue
                    self._set_connected(True)
                    try:
                        stream = sock.makefile("r", encoding="utf-8")
                        for line in stream:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                message = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            self._apply(message)
                    except OSError:
                        pass
                    finally:
                        stream.close()
                        sock.close()
                    self._set_connected(False)
            except KeyboardInterrupt:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket", default=DEFAULT_SOCKET, help="Unix socket the MCP server listens on"
    )
    parser.add_argument("--background", default="#000000", help="fallback color for odd-height canvases")
    args = parser.parse_args()
    PixelRenderer(args.socket, parse_hex(args.background)).run()


if __name__ == "__main__":
    main()
