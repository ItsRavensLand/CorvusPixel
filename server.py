"""CorvusPixel — an MCP server behind an interactive, user-drawn pixel canvas.

The canvas lives in this process. The *user* draws on it with their mouse and
keyboard in a separate terminal window (opened via the :func:`open_canvas`
tool); those edits flow back to this server over a Unix domain socket and are
rebroadcast to every connected window. Claude Code reads the result cheaply with
:func:`see_canvas` and only draws (``set_pixel`` & friends) when the user asks.

Transport split: this server speaks MCP over stdio (Claude Code's default) to
the client, and a separate, session-scoped Unix socket to the canvas windows —
named after this process's parent PID so each Claude Code session gets its own
canvas and never collides with a stale instance.
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# --------------------------------------------------------------------------- #
# Protocol / shared helpers
# --------------------------------------------------------------------------- #

Color = tuple[int, int, int]


def default_socket_path() -> str:
    """Session-scoped socket path for this MCP server instance.

    The path is named after this process's parent PID — the Claude Code session
    that spawned us over stdio. A canvas window opened from this session always
    talks to exactly this server instance, never a stale one from another
    session. ``CORVUSPIXEL_SOCK`` overrides it for manual runs and tests.
    """
    configured = os.environ.get("CORVUSPIXEL_SOCK")
    if configured:
        return configured
    return f"/tmp/corvuspixel-{os.getppid()}.sock"


DEFAULT_WIDTH = 32
DEFAULT_HEIGHT = 32
DEFAULT_BACKGROUND = "#101028"  # deep navy, so bright pixels pop


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


def compact_view(canvas: "PixelCanvas", max_grid: int = 16) -> str:
    """A token-cheap, model-friendly view of the canvas.

    Downsample the canvas to at most ``max_grid`` x ``max_grid`` blocks; each
    block becomes one symbol by majority vote over its non-background pixels.
    The legend maps every symbol back to a hex color. The output is a few short
    lines regardless of canvas size, so the model can read shapes, positions and
    colors without a raw pixel dump.
    """
    background = canvas.background
    step_x = max(1, math.ceil(canvas.width / max_grid))
    step_y = max(1, math.ceil(canvas.height / max_grid))
    grid_w = math.ceil(canvas.width / step_x)
    grid_h = math.ceil(canvas.height / step_y)

    votes: list[list[dict[Color, int]]] = [
        [{} for _ in range(grid_w)] for _ in range(grid_h)
    ]
    for y in range(canvas.height):
        for x in range(canvas.width):
            color = canvas._pixels[y][x]
            if color == background:
                continue
            gy, gx = y // step_y, x // step_x
            bucket = votes[gy][gx]
            bucket[color] = bucket.get(color, 0) + 1

    symbols = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    color_to_symbol: dict[Color, str] = {}
    next_index = 0

    def symbol_for(color: Color) -> str:
        nonlocal next_index
        if color in color_to_symbol:
            return color_to_symbol[color]
        if next_index >= len(symbols):
            return "*"
        symbol = symbols[next_index]
        next_index += 1
        color_to_symbol[color] = symbol
        return symbol

    lines: list[str] = []
    for gy in range(grid_h):
        chars: list[str] = []
        for gx in range(grid_w):
            bucket = votes[gy][gx]
            if not bucket:
                chars.append(".")
            else:
                majority = max(bucket, key=bucket.get)
                chars.append(symbol_for(majority))
        lines.append("".join(chars))

    legend = ", ".join(
        [f". = background {hex_str(background)}"]
        + [f"{sym} = {hex_str(color)}" for color, sym in color_to_symbol.items()]
    )
    scale = f"{step_x}x{step_y} block" if (step_x, step_y) != (1, 1) else "1x1"
    return (
        f"Canvas {canvas.width}x{canvas.height} shown at {grid_w}x{grid_h} "
        f"(each symbol = a {scale}). Origin top-left, 0-based.\n"
        f"Legend: {legend}.\n"
        + "\n".join(lines)
    )


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
    so the caller can push a minimal diff to the windows instead of the whole
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

    def resize(self, new_w: int, new_h: int) -> None:
        """Resize the canvas, keeping the top-left region where it still fits.

        Grown cells take the background color; pixels outside the new size are
        dropped (the canvas grows/shrinks at the right and bottom edges).
        Dimensions must stay positive.
        """
        if new_w <= 0 or new_h <= 0:
            raise ValueError("width and height must be positive")
        if new_w == self.width and new_h == self.height:
            return
        old = self._pixels
        old_w, old_h = self.width, self.height
        self._pixels = [[self.background] * new_w for _ in range(new_h)]
        self.width, self.height = new_w, new_h
        for y in range(min(old_h, new_h)):
            for x in range(min(old_w, new_w)):
                self._pixels[y][x] = old[y][x]

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
# Socket hub: bidirectional link between the server and the canvas windows
# --------------------------------------------------------------------------- #


class RendererSink:
    """Accepts canvas-window connections, pushes diffs, and ingests user edits.

    Each connected window gets a *full* snapshot when it connects — so a
    reconnecting window can never be out of sync — and every subsequent canvas
    change is pushed as an incremental ``update`` message carrying only the
    changed cells. The reverse direction is user input: windows send
    ``{"type": "edit", "changes": [...]}``, which ``on_edit`` delivers to the
    server to apply and rebroadcast, and ``{"type": "resize", width, height}``,
    which ``on_resize`` delivers so the server can keep its canvas in sync.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.RLock()
        self._stop = threading.Event()
        self._snapshot_provider: Callable[[], dict[str, Any]] | None = None
        self._on_edit: Callable[[list[CellChange]], None] | None = None
        self._on_resize: Callable[[int, int], None] | None = None
        self._on_objects: Callable[[list[Any]], None] | None = None
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._reader_threads: set[threading.Thread] = set()

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def start(
        self,
        snapshot_provider: Callable[[], dict[str, Any]],
        on_edit: Callable[[list[CellChange]], None] | None = None,
        on_resize: Callable[[int, int], None] | None = None,
        on_objects: Callable[[list[Any]], None] | None = None,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._on_edit = on_edit
        self._on_resize = on_resize
        self._on_objects = on_objects
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self._socket_path)  # clear a stale socket from a dead run
        except FileNotFoundError:
            pass
        self._server.bind(self._socket_path)
        # The socket path is created with the process umask (usually 0755), which
        # on a multi-user machine would let any local user connect and draw.
        # Restrict it to the owner so only this session's windows can connect.
        os.chmod(self._socket_path, 0o600)
        self._server.listen(4)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="corvuspixel-accept", daemon=True
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
            # Only the new window needs a fresh snapshot — don't re-broadcast a
            # full redraw to windows that already have current state.
            if self._snapshot_provider is not None:
                payload = (
                    json.dumps({"type": "full", **self._snapshot_provider()}) + "\n"
                ).encode("utf-8")
                try:
                    conn.sendall(payload)
                except OSError:
                    pass
            reader = threading.Thread(
                target=self._client_read_loop,
                args=(conn,),
                name="corvuspixel-read",
                daemon=True,
            )
            with self._clients_lock:
                self._reader_threads.add(reader)
            reader.start()

    def _client_read_loop(self, conn: socket.socket) -> None:
        """Read edits from one canvas window and hand them to the server."""
        try:
            stream = conn.makefile("r", encoding="utf-8")
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "edit" and self._on_edit is not None:
                    changes = [
                        CellChange(c["x"], c["y"], tuple(c["color"]))
                        for c in message.get("changes", [])
                    ]
                    self._on_edit(changes)
                elif message.get("type") == "resize" and self._on_resize is not None:
                    w, h = message.get("width"), message.get("height")
                    if isinstance(w, int) and isinstance(h, int):
                        self._on_resize(w, h)
                elif message.get("type") == "objects" and self._on_objects is not None:
                    self._on_objects(message.get("objects", []))
        except OSError:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def push(self, message: dict[str, Any]) -> None:
        """Serialize and broadcast a message to every connected window."""
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

    def live_client_count(self) -> int:
        """How many canvas windows are currently connected to the socket."""
        with self._clients_lock:
            return len(self._clients)

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
            self._reader_threads.clear()


# --------------------------------------------------------------------------- #
# Canvas + socket wiring, one public method per MCP tool
# --------------------------------------------------------------------------- #


class CanvasServer:
    """Owns the canvas and the window socket, and exposes the drawing API."""

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
        # Stored drawing objects (shapes, fills, pixel-text) and terminal-text
        # label objects shared with canvas windows, in z-order = list order
        # (later objects render on top). Each entry is a clean
        # {"id", "kind", "color": [r,g,b], "data": {...}, "pixels": [[x,y],...]}
        # dict; labels carry no pixels (they are a terminal overlay). Kept in
        # sync with every connected window and shown by see_canvas().
        self._objects: list[dict[str, Any]] = []

    def start(self) -> None:
        """Start accepting canvas-window connections (call before ``server.run()``)."""
        self._sink.start(
            self.snapshot,
            on_edit=self._apply_edits,
            on_resize=self._resize_canvas,
            on_objects=self._apply_objects,
        )

    def close(self) -> None:
        self._sink.close()

    @property
    def socket_path(self) -> str:
        """The Unix socket canvas windows connect to (used by ``open_canvas``)."""
        return self._sink.socket_path

    @property
    def canvas_window_open(self) -> bool:
        """True while at least one canvas window is connected to the socket."""
        return self._sink.live_client_count() > 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._canvas.snapshot(), "objects": self._objects}

    # -- user input coming back from a canvas window -------------------------

    def _apply_edits(self, changes: list[CellChange]) -> None:
        """Apply user-drawn edits from a canvas window and rebroadcast them."""
        with self._lock:
            applied: list[CellChange] = []
            for change in changes:
                try:
                    applied.extend(self._canvas.set_pixel(change.x, change.y, change.color))
                except ValueError:
                    continue  # out-of-bounds from a misbehaving window: ignore
            self._publish(applied)

    def _resize_canvas(self, width: int, height: int) -> None:
        """Resize the canvas from a window, preserving pixels that still fit."""
        with self._lock:
            width = max(1, min(width, 100))
            height = max(1, min(height, 100))
            if (width, height) == (self._canvas.width, self._canvas.height):
                return
            self._canvas.resize(width, height)
            self._clip_objects(width, height)
            # Dimensions changed: every window must re-sync from a fresh snapshot.
            self._sink.push({"type": "full", **self.snapshot()})

    def _clip_objects(self, width: int, height: int) -> None:
        """Clip every pixel object's pixels to the new canvas size, dropping
        objects that no longer have any pixels. Labels (a terminal overlay)
        keep their terminal positions."""
        kept: list[dict[str, Any]] = []
        for obj in self._objects:
            if obj["kind"] != "label":
                clipped = [[x, y] for x, y in obj["pixels"]
                           if 0 <= x < width and 0 <= y < height]
                if not clipped:
                    continue
                obj["pixels"] = clipped
            kept.append(obj)
        self._objects = kept

    def _apply_objects(self, objects: list[Any]) -> None:
        """Store the object list sent by a canvas window and rebroadcast it,
        so every connected window (and see_canvas) stays in sync."""
        with self._lock:
            cleaned: list[dict[str, Any]] = []
            for entry in objects:
                obj = self._clean_object(entry)
                if obj is not None:
                    cleaned.append(obj)
            self._objects = cleaned
            self._sink.push({"type": "objects", "objects": self._objects})

    def _clean_object(self, entry: Any) -> dict[str, Any] | None:
        """Validate one wire object dict, or return None if malformed."""
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
            rgb = [int(c) for c in entry["color"]]
        except (TypeError, ValueError):
            return None
        if len(rgb) != 3:
            return None
        data = entry["data"]
        if not isinstance(data, dict):
            return None
        pixels: list[list[int]] = []
        if kind != "label":
            for cell in entry.get("pixels", []):
                if isinstance(cell, (list, tuple)) and len(cell) == 2:
                    try:
                        pixels.append([int(cell[0]), int(cell[1])])
                    except (TypeError, ValueError):
                        continue
        return {"id": oid, "kind": kind, "color": rgb, "data": data, "pixels": pixels}

    def _composite_pixels(self) -> list[list[Color]]:
        """The rendered canvas: the base raster with every object's pixels
        painted on top in z-order (later objects win)."""
        grid = [list(row) for row in self._canvas._pixels]
        for obj in self._objects:
            if obj["kind"] == "label":
                continue
            color = tuple(obj["color"])
            for x, y in obj["pixels"]:
                if 0 <= x < self._canvas.width and 0 <= y < self._canvas.height:
                    grid[y][x] = color
        return grid

    # -- the eight tool bodies ----------------------------------------------

    def reset(self, width: int, height: int, background: Color) -> str:
        with self._lock:
            width = max(1, min(width, 100))  # mirror the resize-path bounds
            height = max(1, min(height, 100))
            self._canvas = PixelCanvas(width, height, background)
            self._objects = []  # a fresh canvas has no objects
            self._sink.push({"type": "full", **self.snapshot()})
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
            if self._objects:
                # A full clear: wipe the objects too, so a fresh full snapshot
                # is the only way to sync every window to the blank canvas.
                self._objects = []
                self._canvas.clear(rgb)
                self._sink.push({"type": "full", **self.snapshot()})
            else:
                self._publish(self._canvas.clear(rgb))
            return f"canvas cleared to {color}"

    def get_canvas(self) -> str:
        """Full state as JSON — the base raster with every object composited on
        top in z-order (debugging)."""
        with self._lock:
            data = self._canvas.snapshot()
            data["pixels"] = [
                [list(c) for c in row] for row in self._composite_pixels()
            ]
            return json.dumps(data)

    def see_canvas(self) -> str:
        """Return a compact, token-cheap view of the canvas for the model."""
        with self._lock:
            composite = PixelCanvas(
                self._canvas.width, self._canvas.height, self._canvas.background
            )
            composite._pixels = self._composite_pixels()
            out = compact_view(composite)
            if self._objects:
                parts = ["objects:"]
                for obj in self._objects:
                    parts.append(self._describe_object(obj))
                out += "\n" + "\n".join(parts)
            return out

    def _describe_object(self, obj: dict[str, Any]) -> str:
        """One type-aware line for see_canvas's objects section."""
        oid = obj["id"]
        hexc = hex_str(tuple(obj["color"]))
        kind = obj["kind"]
        if kind == "shape":
            d = obj["data"]
            return (
                f'  shape {oid}: {d["shape_type"]} '
                f"({d['x1']},{d['y1']})-({d['x2']},{d['y2']}) in {hexc}"
            )
        if kind == "fill":
            return f"  fill {oid}: {len(obj['pixels'])} pixels in {hexc}"
        if kind == "text":
            text = obj["data"].get("text", "").replace("\n", " / ")
            return f'  text {oid}: "{text}" in {hexc}'
        # label: one line per label line of terminal text
        lines = obj["data"].get("lines", [])
        if not lines:
            return f"  label {oid}: (empty) in {hexc}"
        return "  " + ", ".join(
            f'label {oid}: row {row}, col {col}: "{text}" in {hexc}'
            for row, col, text in lines
        )

    def draw_default(self) -> str:
        """Draw the built-in smiley on the current canvas."""
        with self._lock:
            self._publish(draw_smiley(self._canvas))
            return "drew the default smiley"

    def _publish(self, changes: list[CellChange]) -> None:
        if changes:
            self._sink.push({"type": "update", "changes": [c.to_dict() for c in changes]})


# --------------------------------------------------------------------------- #
# Window launcher (used by the open_canvas tool)
# --------------------------------------------------------------------------- #


@dataclass
class LaunchedWindow:
    """Which terminal we opened the canvas in, and the launcher's PID."""

    terminal: str
    pid: int | None


def _launch_canvas_window(socket_path: str) -> LaunchedWindow | None:
    """Open a full OS terminal window running the interactive canvas app.

    Tries, in order: Windows Terminal (Windows), Terminal.app (macOS), and on
    Linux/BSD ``x-terminal-emulator``, ``gnome-terminal``, ``konsole``, then
    ``xterm``. Returns the first that starts, or ``None`` if none is available.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    app = os.path.join(project_dir, "canvas_app.py")
    python = sys.executable
    shell_cmd = f'"{python}" "{app}" --socket "{socket_path}"'

    system = platform.system()

    if system == "Windows":
        wt = shutil.which("wt.exe") or shutil.which("wt")
        if wt is not None:
            try:
                proc = subprocess.Popen([wt, "new-tab", "--", shell_cmd])
                return LaunchedWindow("Windows Terminal", proc.pid)
            except OSError:
                pass
        return None

    if system == "Darwin":
        # Terminal.app runs executable .command files in a fresh window.
        script_path = os.path.join(tempfile.gettempdir(), f"corvuspixel-{os.getpid()}.command")
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write("#!/bin/bash\n")
                f.write(f"cd {shlex.quote(project_dir)} && exec {shell_cmd}\n")
            os.chmod(script_path, 0o755)
            proc = subprocess.Popen(["open", "-a", "Terminal", script_path])
            return LaunchedWindow("Terminal.app", proc.pid)
        except OSError:
            return None

    for terminal in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        exe = shutil.which(terminal)
        if exe is None:
            continue
        if terminal == "gnome-terminal":
            argv = [exe, "--", "bash", "-c", shell_cmd]
        else:
            argv = [exe, "-e", "bash", "-c", shell_cmd]
        try:
            proc = subprocess.Popen(argv, start_new_session=True)
            return LaunchedWindow(terminal, proc.pid)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #

server = MCPServer(
    "CorvusPixel",
    instructions=(
        "Interactive pixel-art canvas. The user draws in a terminal window opened "
        "with open_canvas(); read what they drew with see_canvas(). Coordinates "
        "are 0-based with the origin top-left; colors are '#rrggbb'. Only draw "
        "(set_pixel/fill_rect/draw_line/clear/init_canvas) when the user asks."
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


@server.tool()
def see_canvas() -> str:
    """Read the current canvas as a compact symbol grid — cheap for the model.

    Returns a downsampled text grid (each symbol = one color block) with a
    legend. Use this to see what the user drew; it is far cheaper in tokens than
    a raw pixel dump. Does not modify the canvas.
    """
    return _server().see_canvas()


@server.tool()
def canvas_status() -> str:
    """Report whether a canvas window is currently connected to this session.

    Returns "open" if a canvas window is connected to the session socket,
    "closed" otherwise. Call this before ``open_canvas()`` so you never spawn
    a duplicate window.
    """
    return "open" if _server().canvas_window_open else "closed"


@server.tool()
def open_canvas() -> str:
    """Open a terminal window running the interactive canvas app.

    The window connects to this session's canvas socket. Draw with the mouse
    (a clickable toolbar above the canvas, a brush-size bar, palette swatches,
    an always-visible bottom row of common-colour swatches plus a custom-colour
    swatch that accepts a hex value, and click/click-drag painting) or keyboard
    (arrow keys move the cursor, space paints, x erases, e toggles the eraser,
    +/- changes the brush size, c cycles the palette, 1-8 pick a color, Tab
    opens the visual palette, [ ] / { } grow or shrink the canvas, q quits).
    Changes appear here instantly; read them back with see_canvas(). Fails if
    no compatible terminal is found.
    """
    state = _server()
    launched = _launch_canvas_window(state.socket_path)
    if launched is None:
        raise ToolError(
            "no compatible terminal found — install gnome-terminal, konsole or xterm"
        )
    pid = f" (launcher pid {launched.pid})" if launched.pid else ""
    return (
        f"Opened the canvas in a new {launched.terminal}{pid}. "
        "Mouse: toolbar buttons, brush bar, palette/colour swatches, and "
        "click/drag to paint. "
        "Keys: arrows move, space paint, x erase, e eraser, +/- brush size, "
        "c next color, 1-8 palette, [ ]/ { } resize, q quit."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    global _state

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        default=default_socket_path(),
        help="Unix socket canvas windows connect to (default: session-scoped path)",
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
