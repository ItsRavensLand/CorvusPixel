"""CorvusPixel — an MCP server that drives a live terminal pixel-art renderer.

The canvas lives in this process. Every tool call mutates the canvas and then
pushes *only the changed cells* as a JSON message over a Unix domain socket to a
connected renderer process (:mod:`renderer`), which repaints those cells in a
truecolor terminal using the ``▀`` half-block character — no files, real time.

Transport split: this server speaks MCP over stdio (Claude Code's default) to
the client, and a separate Unix socket to the renderer process.
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# --------------------------------------------------------------------------- #
# Protocol / shared helpers
# --------------------------------------------------------------------------- #

Color = tuple[int, int, int]

DEFAULT_SOCKET = os.environ.get("CORVUSPIXEL_SOCK", "/tmp/corvuspixel.sock")
DEFAULT_WIDTH = 32
DEFAULT_HEIGHT = 32
DEFAULT_BACKGROUND = "#101028"  # deep navy, so the yellow smiley pops


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
# Canvas state
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CellChange:
    """A single pixel that changed value, produced by a canvas operation."""

    x: int
    y: int
    color: Color

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "color": list(self.color)}


class PixelCanvas:
    """A 2-D grid of RGB pixels plus the drawing primitives the tools need.

    Every mutating operation returns the exact set of cells that changed value,
    so the caller can push a minimal diff to the renderer instead of the whole
    canvas. ``set_pixel`` errors on out-of-bounds coordinates; ``fill_rect`` and
    ``draw_line`` clip to the canvas edges.
    """

    def __init__(self, width: int, height: int, background: Color = (0, 0, 0)) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        self.width = width
        self.height = height
        self.background = background
        self._pixels: list[list[Color]] = [[background] * width for _ in range(height)]

    # -- read --------------------------------------------------------------

    def get(self, x: int, y: int) -> Color:
        self._check_bounds(x, y)
        return self._pixels[y][x]

    def snapshot(self) -> dict[str, Any]:
        """JSON-friendly copy of the whole canvas."""
        return {
            "width": self.width,
            "height": self.height,
            "background": list(self.background),
            "pixels": [[list(c) for c in row] for row in self._pixels],
        }

    # -- draw --------------------------------------------------------------

    def set_pixel(self, x: int, y: int, color: Color) -> list[CellChange]:
        self._check_bounds(x, y)
        if self._pixels[y][x] == color:
            return []
        self._pixels[y][x] = color
        return [CellChange(x, y, color)]

    def fill_rect(
        self, x: int, y: int, width: int, height: int, color: Color
    ) -> list[CellChange]:
        """Fill an axis-aligned rectangle, clipped to the canvas edges."""
        changes: list[CellChange] = []
        x1, x2 = max(0, x), min(self.width, x + width)
        y1, y2 = max(0, y), min(self.height, y + height)
        for py in range(y1, y2):
            row = self._pixels[py]
            for px in range(x1, x2):
                if row[px] != color:
                    row[px] = color
                    changes.append(CellChange(px, py, color))
        return changes

    def draw_line(
        self, x1: int, y1: int, x2: int, y2: int, color: Color
    ) -> list[CellChange]:
        """Draw a line with Bresenham's algorithm, clipped to the canvas."""
        changes: list[CellChange] = []
        dx = abs(x2 - x1)
        dy = -abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx + dy
        while True:
            if 0 <= x1 < self.width and 0 <= y1 < self.height:
                if self._pixels[y1][x1] != color:
                    self._pixels[y1][x1] = color
                    changes.append(CellChange(x1, y1, color))
            if x1 == x2 and y1 == y2:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x1 += sx
            if e2 <= dx:
                err += dx
                y1 += sy
        return changes

    def clear(self, color: Color) -> list[CellChange]:
        """Repaint the whole canvas; returns only pixels that actually changed."""
        changes: list[CellChange] = []
        for py in range(self.height):
            row = self._pixels[py]
            for px in range(self.width):
                if row[px] != color:
                    row[px] = color
                    changes.append(CellChange(px, py, color))
        self.background = color
        return changes

    def _check_bounds(self, x: int, y: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"({x}, {y}) is outside the {self.width}x{self.height} canvas")


def draw_smiley(canvas: PixelCanvas) -> list[CellChange]:
    """Draw a smiley with plain ``set_pixel`` calls (the built-in test shape)."""
    yellow = (255, 214, 64)
    black = (0, 0, 0)
    pink = (255, 120, 150)

    changes: list[CellChange] = []

    def disc(cx: int, cy: int, radius: int, color: Color) -> None:
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius:
                    changes.extend(canvas.set_pixel(cx + dx, cy + dy, color))

    cx, cy = canvas.width // 2, canvas.height // 2
    disc(cx, cy, 10, yellow)  # face
    disc(cx - 5, cy - 3, 2, black)  # left eye
    disc(cx + 5, cy - 3, 2, black)  # right eye
    disc(cx - 6, cy + 4, 2, pink)  # left cheek
    disc(cx + 6, cy + 4, 2, pink)  # right cheek
    # smile: a downward arc of black pixels (screen y grows downwards)
    for angle in range(20, 161):
        rad = math.radians(angle)
        x = round(cx + 6 * math.cos(rad))
        y = round(cy + 5 * math.sin(rad))
        changes.extend(canvas.set_pixel(x, y, black))

    return changes


# --------------------------------------------------------------------------- #
# Socket sink: pushes diffs to every connected renderer process
# --------------------------------------------------------------------------- #


class RendererSink:
    """Accepts renderer connections on a Unix socket and broadcasts messages.

    Each renderer gets a *full* snapshot when it connects — so a reconnecting
    renderer can never be out of sync — and every subsequent canvas change is
    pushed as an incremental ``update`` message carrying only the changed cells.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.RLock()
        self._stop = threading.Event()
        self._snapshot_provider: Callable[[], dict[str, Any]] | None = None
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self, snapshot_provider: Callable[[], dict[str, Any]]) -> None:
        self._snapshot_provider = snapshot_provider
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self._socket_path)  # clear a stale socket from a dead run
        except FileNotFoundError:
            pass
        self._server.bind(self._socket_path)
        self._server.listen(4)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="corvuspixel-renderer-accept", daemon=True
        )
        self._accept_thread.start()
        atexit.register(self.close)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                if self._stop.is_set():
                    break
                continue
            with self._clients_lock:
                self._clients.add(conn)
            if self._snapshot_provider is not None:
                self.push({"type": "full", **self._snapshot_provider()})

    def push(self, message: dict[str, Any]) -> None:
        """Serialize and broadcast a message to every connected renderer."""
        payload = (json.dumps(message) + "\n").encode("utf-8")
        with self._clients_lock:
            dead: list[socket.socket] = []
            for conn in self._clients:
                try:
                    conn.sendall(payload)
                except OSError:
                    dead.append(conn)
            for conn in dead:
                self._clients.discard(conn)
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass
        with self._clients_lock:
            for conn in self._clients:
                try:
                    conn.close()
                except OSError:
                    pass
            self._clients.clear()


# --------------------------------------------------------------------------- #
# Canvas + sink wiring, one public method per MCP tool
# --------------------------------------------------------------------------- #


class CanvasServer:
    """Owns the canvas and the renderer socket, and exposes the drawing API."""

    def __init__(
        self,
        socket_path: str,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        background: Color = parse_hex(DEFAULT_BACKGROUND),
    ) -> None:
        self._lock = threading.RLock()
        self._canvas = PixelCanvas(width, height, background)
        self._sink = RendererSink(socket_path)

    def start(self) -> None:
        """Start accepting renderer connections (call before ``server.run()``)."""
        self._sink.start(self.snapshot)

    def close(self) -> None:
        self._sink.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._canvas.snapshot()

    # -- the six tool bodies -------------------------------------------------

    def reset(self, width: int, height: int, background: Color) -> str:
        with self._lock:
            self._canvas = PixelCanvas(width, height, background)
            self._sink.push({"type": "full", **self._canvas.snapshot()})
            return (
                f"canvas initialized to {width}x{height} "
                f"with background {hex_str(background)}"
            )

    def set_pixel(self, x: int, y: int, color: str) -> str:
        with self._lock:
            rgb = parse_hex(color)
            self._publish(self._canvas.set_pixel(x, y, rgb))
            return f"set ({x}, {y}) to {color}"

    def fill_rect(self, x: int, y: int, width: int, height: int, color: str) -> str:
        with self._lock:
            rgb = parse_hex(color)
            self._publish(self._canvas.fill_rect(x, y, width, height, rgb))
            return f"filled {width}x{height} rect at ({x}, {y}) with {color}"

    def draw_line(self, x1: int, y1: int, x2: int, y2: int, color: str) -> str:
        with self._lock:
            rgb = parse_hex(color)
            self._publish(self._canvas.draw_line(x1, y1, x2, y2, rgb))
            return f"drew line ({x1},{y1}) -> ({x2},{y2}) in {color}"

    def clear(self, color: str) -> str:
        with self._lock:
            rgb = parse_hex(color)
            self._publish(self._canvas.clear(rgb))
            return f"canvas cleared to {color}"

    def get_canvas(self) -> str:
        with self._lock:
            return json.dumps(self._canvas.snapshot())

    def draw_default(self) -> str:
        """Draw the built-in smiley on the current canvas."""
        with self._lock:
            self._publish(draw_smiley(self._canvas))
            return "drew the default smiley"

    def _publish(self, changes: list[CellChange]) -> None:
        if changes:
            self._sink.push({"type": "update", "changes": [c.to_dict() for c in changes]})


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #

server = MCPServer(
    "CorvusPixel",
    instructions=(
        "Live pixel-art canvas. Coordinates are 0-based with the origin at the "
        "top-left; x grows right, y grows down. Colors are '#rrggbb' hex strings. "
        "Every change is drawn immediately in the CorvusPixel terminal pane. "
        "The canvas starts at 32x32 with a smiley already drawn on it."
    ),
)

_state: CanvasServer | None = None


def _server() -> CanvasServer:
    if _state is None:
        raise RuntimeError("CorvusPixel server has not been started yet")
    return _state


def _tool_result(fn: Callable[[], str]) -> str:
    """Run a tool body, converting user-facing ``ValueError``s into MCP errors."""
    try:
        return fn()
    except ValueError as exc:
        raise ToolError(str(exc)) from None


@server.tool()
def init_canvas(
    width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, color: str = DEFAULT_BACKGROUND
) -> str:
    """Create a fresh canvas of the given size, filled with a background color.

    Args:
        width: Canvas width in pixels.
        height: Canvas height in pixels.
        color: Background color as '#rrggbb'.
    """
    return _tool_result(lambda: _server().reset(width, height, parse_hex(color)))


@server.tool()
def set_pixel(x: int, y: int, color: str) -> str:
    """Set a single pixel to a color.

    Args:
        x: Column (0-based, grows right).
        y: Row (0-based, grows down).
        color: '#rrggbb' hex string.
    """
    return _tool_result(lambda: _server().set_pixel(x, y, color))


@server.tool()
def fill_rect(x: int, y: int, width: int, height: int, color: str) -> str:
    """Fill an axis-aligned rectangle (clipped to the canvas edges).

    Args:
        x: Left edge (0-based).
        y: Top edge (0-based).
        width: Rectangle width in pixels.
        height: Rectangle height in pixels.
        color: '#rrggbb' hex string.
    """
    return _tool_result(lambda: _server().fill_rect(x, y, width, height, color))


@server.tool()
def draw_line(x1: int, y1: int, x2: int, y2: int, color: str) -> str:
    """Draw a line between two points (Bresenham, clipped to the canvas).

    Args:
        x1, y1: Start point.
        x2, y2: End point.
        color: '#rrggbb' hex string.
    """
    return _tool_result(lambda: _server().draw_line(x1, y1, x2, y2, color))


@server.tool()
def clear(color: str = DEFAULT_BACKGROUND) -> str:
    """Clear the whole canvas to a solid color.

    Args:
        color: '#rrggbb' hex string to repaint every pixel with.
    """
    return _tool_result(lambda: _server().clear(color))


@server.tool()
def get_canvas() -> str:
    """Return the full canvas state as JSON — width, height and pixel grid (debugging)."""
    return _server().get_canvas()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    global _state

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket", default=DEFAULT_SOCKET, help="Unix socket the renderer connects to"
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--background", default=DEFAULT_BACKGROUND)
    args = parser.parse_args()

    _state = CanvasServer(args.socket, args.width, args.height, parse_hex(args.background))
    _state.start()
    _state.draw_default()
    try:
        server.run(transport="stdio")
    finally:
        _state.close()


if __name__ == "__main__":
    main()
