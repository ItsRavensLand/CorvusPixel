"""CorvusPixel interactive canvas — the window the *user* draws in.

This replaces the old passive renderer. It connects to the MCP server's
session-scoped Unix socket, renders the shared canvas live (diff-only, truecolor
``▀`` half-blocks — each terminal row shows two canvas rows), and lets the user
draw:

- **Keyboard**: arrow keys move the cursor; ``space`` paints/toggles the pixel
  under the cursor to the current color; ``x`` erases it; ``c`` cycles the
  palette; ``1``-``8`` pick a palette color directly; ``q`` or Ctrl+C quits.
- **Mouse**: click (or click-drag) paints the clicked cell, via SGR mouse-mode
  reporting.

Every user edit is applied locally for instant feedback (no round-trip wait)
*and* written back to the server as ``{"type": "edit", "changes": [...]}``. The
server is the single source of truth: it applies the edit and rebroadcasts an
``update`` to every connected window.

Why raw ANSI instead of textual? The canvas is a grid of half-block cells that
must repaint only the cells that changed — no full-region redraws. That is the
core rendering requirement here, and it is simplest with our own ANSI cell-diff
on top of rich's ``Live`` lifecycle context. Terminal input (arrow keys, SGR
mouse) arrives as byte sequences on stdin, which we parse directly; pulling in
a full widget framework (textual) would add a large dependency without helping
the diff-only renderer.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import termios
import threading
import time
import tty
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

# The brush palette: (name, color). Keys 1-8 select these; 'c' cycles.
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
# The interactive canvas app
# --------------------------------------------------------------------------- #


class CanvasApp:
    """Draws the canvas and turns keyboard/mouse input into pixel edits.

    ``console`` and ``input_stream`` are injectable so tests can capture output
    and drive input without a real terminal.
    """

    def __init__(
        self,
        socket_path: str,
        background: Color = (0, 0, 0),
        console: Console | None = None,
        input_stream: Any = None,
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
        self._color = PALETTE[0][1]

        self._sock: socket.socket | None = None
        self._quit = threading.Event()
        self._lock = threading.RLock()
        self._input_stream = input_stream if input_stream is not None else sys.stdin
        self._old_termios: Any = None

        self._console = console or Console(color_system="truecolor")
        # What is currently drawn on screen (row 1 = header, rows 2+ = canvas,
        # row len(rows)+2 = palette/help line).
        self._old_cells: list[list[Cell]] = []
        self._old_header = ""
        self._old_status = ""
        self._old_cursor_cell: tuple[int, int] | None = None

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
        self._cursor_x = min(self._cursor_x, max(0, width - 1))
        self._cursor_y = min(self._cursor_y, max(0, height - 1))

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

    def _cursor_cell(self) -> tuple[int, int] | None:
        """The display cell the cursor is on: (row index, column)."""
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

    def _render_header(self) -> str:
        status = (
            f"CorvusPixel  {self._width}x{self._height}  "
            f"{'● connected' if self._connected else '○ reconnecting…'}   "
            f"cursor ({self._cursor_x},{self._cursor_y})   "
            f"color {hex_str(self._color)}"
        )
        return self._styled(status, "bold #7fd4ff")

    def _render_status(self) -> str:
        palette = "  ".join(
            ("➤" if i == self._palette_idx else str(i + 1)) + " " + hex_str(color)
            for i, (name, color) in enumerate(PALETTE)
        )
        return (
            "keys: arrows move · space paint/toggle · x erase · "
            f"c next color · q quit   |   {palette}"
        )

    def _draw(self) -> None:
        """Repaint only the header/cells/status that actually changed on screen."""
        with self._lock:
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

            # Cursor overlay: reverse-video the cell under the cursor. When it
            # moves, redraw the old cell plain and the new cell reversed.
            new_cursor = self._cursor_cell()
            if new_cursor != self._old_cursor_cell:
                if self._old_cursor_cell is not None:
                    cy, cx = self._old_cursor_cell
                    if cy < len(old) and cy < len(rows) and cx < len(old[cy]):
                        self._write_cell(file, cy, cx, old[cy][cx], reverse=False)
                if new_cursor is not None:
                    cy, cx = new_cursor
                    if cy < len(rows) and cx < len(rows[cy]):
                        self._write_cell(file, cy, cx, rows[cy][cx], reverse=True)
            self._old_cursor_cell = new_cursor

            self._old_cells = rows

            status = self._render_status()
            if status != self._old_status:
                file.write(f"\x1b[{len(rows) + 2};1H\x1b[2K")
                file.write(status)
                file.write(RESET)
                self._old_status = status

            # Park the cursor below everything so it never wanders.
            file.write(f"\x1b[{len(rows) + 3};1H")
            file.flush()

    @staticmethod
    def _write_cell(file: Any, y: int, x: int, cell: Cell, reverse: bool = False) -> None:
        """Move to a cell and draw it: fg = top pixel, bg = bottom pixel."""
        (fr, fg, fb), (br, bg, bb) = cell
        file.write(f"\x1b[{y + 2};{x + 1}H")  # row 1 is the header
        file.write(f"\x1b[38;2;{fr};{fg};{fb}m")
        file.write(f"\x1b[48;2;{br};{bg};{bb}m")
        if reverse:
            file.write("\x1b[7m")  # reverse video marks the cursor cell
        file.write(HALF_BLOCK)
        file.write(RESET)

    # -- input handling ---------------------------------------------------------

    def _paint_pixel(self, x: int, y: int, color: Color) -> dict[str, Any] | None:
        """Set one canvas pixel locally; returns its change or None if a no-op."""
        if self._pixels[y][x] == color:
            return None
        self._pixels[y][x] = color
        return {"x": x, "y": y, "color": list(color)}

    def _move_cursor(self, dx: int, dy: int) -> None:
        if self._width:
            self._cursor_x = min(max(self._cursor_x + dx, 0), self._width - 1)
        if self._height:
            self._cursor_y = min(max(self._cursor_y + dy, 0), self._height - 1)

    def _cycle_color(self) -> None:
        self._palette_idx = (self._palette_idx + 1) % len(PALETTE)
        self._color = PALETTE[self._palette_idx][1]

    def _select_color(self, index: int) -> None:
        self._palette_idx = index % len(PALETTE)
        self._color = PALETTE[self._palette_idx][1]

    def _toggle_paint(self) -> list[dict[str, Any]]:
        """Paint the cursor pixel with the brush; if already that color, erase."""
        x, y = self._cursor_x, self._cursor_y
        current = self._pixels[y][x]
        color = self._background if current == self._color else self._color
        change = self._paint_pixel(x, y, color)
        return [change] if change else []

    def _erase(self) -> list[dict[str, Any]]:
        change = self._paint_pixel(self._cursor_x, self._cursor_y, self._background)
        return [change] if change else []

    def _handle_mouse(
        self, button: int, col: int, row: int, pressed: bool
    ) -> list[dict[str, Any]]:
        """SGR mouse event. Paints the clicked display cell (both halves)."""
        if not pressed:
            return []
        # Buttons: 0 = left press, 32 = left button held while dragging.
        if button not in (0, 32):
            return []
        # Mouse coords are 1-based terminal rows; row 1 is the header.
        cell_col = col - 1
        display_row = row - 2
        if display_row < 0 or cell_col < 0 or cell_col >= self._width:
            return []
        changes: list[dict[str, Any]] = []
        for yy in (display_row * 2, display_row * 2 + 1):
            if yy >= self._height:
                continue
            change = self._paint_pixel(cell_col, yy, self._color)
            if change is not None:
                changes.append(change)
        return changes

    def _handle_char(self, ch: str) -> list[dict[str, Any]]:
        """Handle a plain (non-escape) key. Returns the pixel changes, if any."""
        if ch == "q":
            self._quit.set()
            return []
        if ch == " ":
            return self._toggle_paint()
        if ch == "x":
            return self._erase()
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
            dx, dy = {"A": (0, -1), "B": (0, 1), "C": (1, 0), "D": (-1, 0)}[final]
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
        """Apply changes locally (done already), redraw, and sync to the server."""
        with self._lock:
            self._draw()
            self._send_edit(changes)

    def _send_edit(self, changes: list[dict[str, Any]]) -> None:
        if not changes or self._sock is None:
            return
        payload = (json.dumps({"type": "edit", "changes": changes}) + "\n").encode("utf-8")
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

    def run(self) -> None:
        """Connect (retrying forever), draw the canvas, and pump user input."""
        raw = self._enter_raw_mode()
        self._enable_mouse()
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
                        continue
                    self._sock = sock
                    self._set_connected(True)
                    try:
                        sock.settimeout(0.2)
                        stream = sock.makefile("r", encoding="utf-8")
                        while not self._quit.is_set():
                            try:
                                line = stream.readline()
                            except socket.timeout:
                                continue  # idle: re-check the quit flag
                            except OSError:
                                break
                            if not line:
                                break
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
                        self._sock = None
                        try:
                            stream.close()
                        except (OSError, UnboundLocalError):
                            pass
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
