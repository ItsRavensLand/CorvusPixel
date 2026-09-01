"""End-to-end tests: canvas -> socket protocol, renderer cell-diffing, MCP stdio.

Run with:  .venv/bin/python -m pytest
"""

import asyncio
import io
import json
import os
import socket
import stat
import tempfile
import time
from pathlib import Path

from rich.console import Console

from server import CanvasServer
from canvas_app import CanvasApp

PROJECT_DIR = Path(__file__).resolve().parents[1]


class FakeRenderer:
    """Mimics canvas_app.py: connects to the server socket and collects messages."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._stream = None

    def connect(self, timeout: float = 5.0) -> None:
        """Connect, retrying until the server has bound the socket."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.settimeout(timeout)
                self._sock.connect(self._socket_path)
                self._stream = self._sock.makefile("r", encoding="utf-8")
                return
            except OSError:
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def read_message(self, timeout: float = 2.0) -> dict:
        self._sock.settimeout(timeout)
        line = self._stream.readline()
        if not line:
            raise TimeoutError("renderer socket closed before a message arrived")
        return json.loads(line)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        if self._sock is not None:
            self._sock.close()


# --------------------------------------------------------------------------- #
# 1. Socket protocol, driven through CanvasServer directly
# --------------------------------------------------------------------------- #


def test_sink_protocol_direct() -> None:
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "corvuspixel.sock")
        cs = CanvasServer(sock_path, width=8, height=8, background=(0, 0, 0))
        cs.start()
        fake = FakeRenderer(sock_path)
        try:
            fake.connect()

            # connecting renderer gets a full snapshot
            msg = fake.read_message()
            assert msg["type"] == "full"
            assert msg["width"] == 8 and msg["height"] == 8
            assert msg["pixels"][0][0] == [0, 0, 0]

            # set_pixel pushes an update with exactly one changed cell
            cs.set_pixel(3, 4, "#ff0000")
            msg = fake.read_message()
            assert msg["type"] == "update"
            assert msg["changes"] == [{"x": 3, "y": 4, "color": [255, 0, 0]}]

            # a no-op set_pixel pushes nothing; the next real change is the only message
            cs.set_pixel(3, 4, "#ff0000")  # no-op
            cs.set_pixel(0, 0, "#00ff00")
            msg = fake.read_message()
            assert msg["type"] == "update"
            assert msg["changes"] == [{"x": 0, "y": 0, "color": [0, 255, 0]}]

            # init_canvas resets -> a fresh full snapshot
            cs.reset(4, 2, (10, 20, 30))
            msg = fake.read_message()
            assert msg["type"] == "full"
            assert msg["width"] == 4 and msg["height"] == 2
            assert msg["background"] == [10, 20, 30]
            assert msg["pixels"][0][0] == [10, 20, 30]
        finally:
            cs.close()
            fake.close()


def test_canvas_status_reflects_live_windows() -> None:
    """canvas_status backs the /canvas duplicate-window guard: it is "closed"
    until a window connects, "open" while one is connected, "closed" again as
    soon as it disconnects."""
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "status.sock")
        cs = CanvasServer(sock_path, width=8, height=8, background=(0, 0, 0))
        cs.start()
        fake = FakeRenderer(sock_path)
        try:
            assert cs.canvas_window_open is False

            fake.connect()
            fake.read_message()  # initial full snapshot
            assert cs.canvas_window_open is True

            fake.close()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and cs.canvas_window_open:
                time.sleep(0.05)
            assert cs.canvas_window_open is False
        finally:
            cs.close()
            fake.close()


def test_socket_file_is_owner_only() -> None:
    """The session socket is chmod'ed to 0600 right after bind(), so other
    local users on a multi-user system cannot connect to it."""
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "perms.sock")
        cs = CanvasServer(sock_path, width=4, height=4, background=(0, 0, 0))
        cs.start()
        try:
            mode = stat.S_IMODE(os.stat(sock_path).st_mode)
            assert mode == 0o600
        finally:
            cs.close()


def test_server_applies_user_edits_and_broadcasts() -> None:
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "edits.sock")
        cs = CanvasServer(sock_path, width=8, height=8, background=(0, 0, 0))
        cs.start()
        fake = FakeRenderer(sock_path)
        try:
            fake.connect()
            fake.read_message()  # the initial full snapshot

            # The interactive app is another client: connect and send an edit.
            app = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            app.connect(sock_path)
            app.sendall(
                (
                    json.dumps(
                        {
                            "type": "edit",
                            "changes": [{"x": 4, "y": 5, "color": [255, 0, 0]}],
                        }
                    )
                    + "\n"
                ).encode()
            )

            # The edit is rebroadcast to the other client as an update.
            msg = fake.read_message()
            assert msg["type"] == "update"
            assert msg["changes"] == [{"x": 4, "y": 5, "color": [255, 0, 0]}]

            # The shared canvas reflects the user's edit.
            assert json.loads(cs.get_canvas())["pixels"][5][4] == [255, 0, 0]
            app.close()
        finally:
            cs.close()
            fake.close()


def test_see_canvas_reflects_user_edits() -> None:
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "see.sock")
        cs = CanvasServer(sock_path, width=4, height=4, background=(0, 0, 0))
        cs.start()
        try:
            cs.set_pixel(0, 0, "#ff0000")
            out = cs.see_canvas()
            assert "a = #ff0000" in out
            assert "background #000000" in out
            assert out.split("\n")[2].startswith("a")  # top-left block is red
        finally:
            cs.close()


def test_server_resizes_canvas_from_window_message() -> None:
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "resize.sock")
        cs = CanvasServer(sock_path, width=4, height=4, background=(0, 0, 0))
        cs.start()
        fake = FakeRenderer(sock_path)
        try:
            fake.connect()
            fake.read_message()  # the initial full snapshot

            cs.set_pixel(0, 0, "#ff0000")
            fake.read_message()  # the update

            # A window asks to grow the canvas at the right/bottom edges.
            app = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            app.connect(sock_path)
            app.sendall(
                (json.dumps({"type": "resize", "width": 6, "height": 5}) + "\n").encode()
            )

            # The resize is rebroadcast as a fresh full snapshot.
            msg = fake.read_message()
            assert msg["type"] == "full"
            assert msg["width"] == 6 and msg["height"] == 5
            assert msg["pixels"][0][0] == [255, 0, 0]  # preserved
            assert msg["pixels"][0][5] == [0, 0, 0]  # new column = background

            data = json.loads(cs.get_canvas())
            assert data["width"] == 6 and data["height"] == 5
            assert data["pixels"][0][0] == [255, 0, 0]
            out = cs.see_canvas()  # compact view reflects the new size
            assert "Canvas 6x5" in out
            app.close()
        finally:
            cs.close()
            fake.close()


def test_objects_sync_over_socket_and_see_canvas() -> None:
    """Objects flow window -> server -> other windows, and show in see_canvas."""
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "objects.sock")
        cs = CanvasServer(sock_path, width=8, height=8, background=(0, 0, 0))
        cs.start()
        fake = FakeRenderer(sock_path)
        try:
            fake.connect()
            initial = fake.read_message()
            assert initial["type"] == "full"
            assert initial["objects"] == []

            # A window broadcasts its object list; the server stores it and
            # pushes an objects message to every connected window.
            app = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            app.connect(sock_path)
            app.sendall(
                (json.dumps(
                    {"type": "objects",
                     "objects": [
                         {"id": 1, "kind": "shape", "color": [255, 0, 0],
                          "data": {"shape_type": "filled_rect", "x1": 0, "y1": 0,
                                   "x2": 3, "y2": 3, "thickness": 1},
                          "pixels": [[0, 0], [1, 1], [2, 2]]},
                         {"id": 2, "kind": "label", "color": [0, 255, 0],
                          "data": {"lines": [[0, 1, "HI"]], "cursor": [0, 0]},
                          "pixels": []},
                     ]}
                ) + "\n").encode()
            )
            msg = fake.read_message()
            assert msg["type"] == "objects"
            assert msg["objects"] == [
                {"id": 1, "kind": "shape", "color": [255, 0, 0],
                 "data": {"shape_type": "filled_rect", "x1": 0, "y1": 0,
                          "x2": 3, "y2": 3, "thickness": 1},
                 "pixels": [[0, 0], [1, 1], [2, 2]]},
                {"id": 2, "kind": "label", "color": [0, 255, 0],
                 "data": {"lines": [[0, 1, "HI"]], "cursor": [0, 0]},
                 "pixels": []},
            ]

            # see_canvas lists every object by id with type-aware info.
            out = cs.see_canvas()
            assert "objects:" in out
            assert "shape 1: filled_rect (0,0)-(3,3) in #ff0000" in out
            assert 'label 2: row 0, col 1: "HI" in #00ff00' in out

            # get_canvas composites the object pixels over the base raster.
            data = json.loads(cs.get_canvas())
            assert data["pixels"][0][0] == [255, 0, 0]  # shape red
            assert data["pixels"][1][1] == [255, 0, 0]  # shape red
            assert data["pixels"][2][2] == [255, 0, 0]  # shape red
            assert data["pixels"][0][1] == [0, 0, 0]  # label has no pixels

            # A connecting window gets the objects in its fresh full snapshot.
            fake.close()
            fake2 = FakeRenderer(sock_path)
            fake2.connect()
            full = fake2.read_message()
            assert full["objects"] == [
                {"id": 1, "kind": "shape", "color": [255, 0, 0],
                 "data": {"shape_type": "filled_rect", "x1": 0, "y1": 0,
                          "x2": 3, "y2": 3, "thickness": 1},
                 "pixels": [[0, 0], [1, 1], [2, 2]]},
                {"id": 2, "kind": "label", "color": [0, 255, 0],
                 "data": {"lines": [[0, 1, "HI"]], "cursor": [0, 0]},
                 "pixels": []},
            ]

            # init_canvas resets the objects along with the pixels.
            cs.reset(4, 4, (5, 5, 5))
            assert "objects:" not in cs.see_canvas()
            app.close()
            fake2.close()
        finally:
            cs.close()
            fake.close()


def test_see_canvas_reports_every_object_type_by_id():
    """see_canvas lists each object kind (shape, fill, text, label) by id with
    type-aware details; get_canvas composites all of them over the base."""
    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "types.sock")
        cs = CanvasServer(sock_path, width=16, height=8, background=(0, 0, 0))
        cs.start()
        try:
            cs._apply_objects([
                {"id": 1, "kind": "shape", "color": [255, 0, 0],
                 "data": {"shape_type": "filled_rect", "x1": 0, "y1": 0,
                          "x2": 2, "y2": 2, "thickness": 1},
                 "pixels": [[0, 0], [1, 0], [2, 0], [0, 1], [1, 1], [2, 1],
                            [0, 2], [1, 2], [2, 2]]},
                {"id": 2, "kind": "fill", "color": [0, 255, 0],
                 "data": {"pixel_count": 3},
                 "pixels": [[4, 4], [4, 5], [5, 5]]},
                {"id": 3, "kind": "text", "color": [0, 0, 255],
                 "data": {"text": "HI"}, "pixels": [[0, 6], [1, 6]]},
                {"id": 4, "kind": "label", "color": [255, 255, 0],
                 "data": {"lines": [[1, 8, "yo"]]}, "pixels": []},
            ])
            out = cs.see_canvas()
            assert "objects:" in out
            assert "shape 1: filled_rect (0,0)-(2,2) in #ff0000" in out
            assert "fill 2: 3 pixels in #00ff00" in out
            assert 'text 3: "HI" in #0000ff' in out
            assert 'label 4: row 1, col 8: "yo" in #ffff00' in out
            # get_canvas composites object pixels over the base; labels have none
            data = json.loads(cs.get_canvas())
            assert data["pixels"][1][0] == [255, 0, 0]   # shape at (0,1)
            assert data["pixels"][4][4] == [0, 255, 0]   # fill at (4,4)
            assert data["pixels"][6][1] == [0, 0, 255]   # text at (1,6)
            assert data["pixels"][1][8] == [0, 0, 0]     # label paints nothing
        finally:
            cs.close()


# --------------------------------------------------------------------------- #
# 2. Renderer: applies messages and draws only the cells that changed
# --------------------------------------------------------------------------- #


def test_renderer_draws_initial_full_state() -> None:
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=80, height=40, force_interactive=True, legacy_windows=False,
    )
    r = CanvasApp(
        "/tmp/nonexistent.sock", background=(0, 0, 0), console=console,
        size_provider=lambda: (80, 40),
    )
    r._blink_active = True
    r._apply({
        "type": "full",
        "width": 4,
        "height": 2,
        "background": [0, 0, 0],
        "pixels": [
            [[255, 0, 0], [255, 0, 0], [255, 0, 0], [255, 0, 0]],
            [[0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]],
        ],
    })
    rendered = out.getvalue()
    assert "CorvusPixel" in rendered  # header
    # 4 cells x 2 columns + 1 cursor cell x 2 + 8 palette swatches x 4 +
    # 8 quick-colour swatches x 4 + the 4-column rainbow custom swatch
    assert rendered.count("▀") == 8 + 2 + 32 + 32 + 4
    assert "\x1b[38;2;255;0;0m" in rendered  # top pixel = red foreground
    assert "\x1b[48;2;0;0;0m" in rendered  # bottom pixel = black background
    # first canvas cell sits at the centered position (row 19, col 37)
    l = r._layout_info
    assert f"\x1b[{l['top_pad'] + 3};{l['left_pad'] + 1}H" in rendered


def test_renderer_rewrites_only_changed_cell() -> None:
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=80, height=40, force_interactive=True, legacy_windows=False,
    )
    r = CanvasApp(
        "/tmp/nonexistent.sock", background=(0, 0, 0), console=console,
        size_provider=lambda: (80, 40),
    )
    r._blink_active = True
    full = {
        "type": "full",
        "width": 4,
        "height": 2,
        "background": [0, 0, 0],
        "pixels": [
            [[255, 0, 0], [255, 0, 0], [255, 0, 0], [255, 0, 0]],
            [[0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]],
        ],
    }
    r._apply(full)
    first = out.getvalue()

    # change pixel (0,0) from red to blue: only that display cell is rewritten
    r._apply({"type": "update", "changes": [{"x": 0, "y": 0, "color": [0, 0, 255]}]})
    delta = out.getvalue()[len(first):]
    l = r._layout_info
    assert delta.count("▀") == 2  # one cell, two half-block columns
    assert "\x1b[38;2;0;0;255m" in delta  # the new blue foreground
    assert f"\x1b[{l['top_pad'] + 3};{l['left_pad'] + 1}H" in delta  # centered cell


# --------------------------------------------------------------------------- #
# 3. The real MCP server over stdio, driven by the official client library
# --------------------------------------------------------------------------- #


def test_mcp_server_stdio_end_to_end() -> None:
    asyncio.run(_run_e2e())


async def _run_e2e() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    import sys

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "e2e.sock")
        fake = FakeRenderer(sock_path)
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=["server.py", "--socket", sock_path],
                cwd=str(PROJECT_DIR),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools_result = await session.list_tools()
                    names = [t.name for t in tools_result.tools]
                    assert names == [
                        "init_canvas",
                        "set_pixel",
                        "fill_rect",
                        "draw_line",
                        "clear",
                        "get_canvas",
                        "see_canvas",
                        "canvas_status",
                        "open_canvas",
                    ]

                    # canvas_status reports no window before anything connects
                    result = await session.call_tool("canvas_status", {})
                    assert _tool_text(result) == "closed"

                    # the fake renderer connects while the server is up: full snapshot
                    fake.connect()
                    msg = fake.read_message()
                    assert msg["type"] == "full"
                    assert msg["width"] == 32 and msg["height"] == 32

                    result = await session.call_tool("canvas_status", {})
                    assert _tool_text(result) == "open"

                    result = await session.call_tool(
                        "set_pixel", {"x": 5, "y": 5, "color": "#00ff00"}
                    )
                    assert "set" in _tool_text(result)

                    msg = fake.read_message()
                    assert msg["type"] == "update"
                    assert msg["changes"] == [{"x": 5, "y": 5, "color": [0, 255, 0]}]

                    result = await session.call_tool("get_canvas", {})
                    data = json.loads(_tool_text(result))
                    assert data["pixels"][5][5] == [0, 255, 0]

                    # out-of-bounds set_pixel surfaces as a tool error, not a crash
                    result = await session.call_tool(
                        "set_pixel", {"x": 99, "y": 99, "color": "#ffffff"}
                    )
                    assert result.is_error

                    # a fresh canvas at an explicit size (the /canvas WxH path)
                    result = await session.call_tool(
                        "init_canvas", {"width": 64, "height": 64}
                    )
                    assert "64x64" in _tool_text(result)
                    result = await session.call_tool("get_canvas", {})
                    data = json.loads(_tool_text(result))
                    assert data["width"] == 64 and data["height"] == 64

                    # a disconnected window flips canvas_status back to closed
                    fake.close()
                    deadline = time.monotonic() + 2.0
                    status = "open"
                    while time.monotonic() < deadline:
                        result = await session.call_tool("canvas_status", {})
                        status = _tool_text(result)
                        if status == "closed":
                            break
                        await asyncio.sleep(0.05)
                    assert status == "closed"
        finally:
            fake.close()


def _tool_text(result) -> str:
    return "".join(item.text for item in result.content if hasattr(item, "text"))
