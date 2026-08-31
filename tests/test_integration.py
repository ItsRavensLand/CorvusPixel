"""End-to-end tests: canvas -> socket protocol, renderer cell-diffing, MCP stdio.

Run with:  .venv/bin/python -m pytest
"""

import asyncio
import io
import json
import socket
import tempfile
import time
from pathlib import Path

from rich.console import Console

from server import CanvasServer
from renderer import PixelRenderer

PROJECT_DIR = Path(__file__).resolve().parents[1]


class FakeRenderer:
    """Mimics renderer.py: connects to the server socket and collects messages."""

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


# --------------------------------------------------------------------------- #
# 2. Renderer: applies messages and draws only the cells that changed
# --------------------------------------------------------------------------- #


def test_renderer_draws_initial_full_state() -> None:
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=80, height=40, force_interactive=True, legacy_windows=False,
    )
    r = PixelRenderer("/tmp/nonexistent.sock", background=(0, 0, 0), console=console)
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
    assert rendered.count("▀") == 4  # 4 cells, one per column
    assert "\x1b[38;2;255;0;0m" in rendered  # top pixel = red foreground
    assert "\x1b[48;2;0;0;0m" in rendered  # bottom pixel = black background


def test_renderer_rewrites_only_changed_cell() -> None:
    out = io.StringIO()
    console = Console(
        file=out, force_terminal=True, color_system="truecolor",
        width=80, height=40, force_interactive=True, legacy_windows=False,
    )
    r = PixelRenderer("/tmp/nonexistent.sock", background=(0, 0, 0), console=console)
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
    assert delta.count("▀") == 1  # exactly one cell redrawn, not the whole canvas
    assert "\x1b[38;2;0;0;255m" in delta  # the new blue foreground
    assert "\x1b[2;1H" in delta  # positioned at the first canvas row's first cell


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
                    ]

                    # the fake renderer connects while the server is up: full snapshot
                    fake.connect()
                    msg = fake.read_message()
                    assert msg["type"] == "full"
                    assert msg["width"] == 32 and msg["height"] == 32

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
        finally:
            fake.close()


def _tool_text(result) -> str:
    return "".join(item.text for item in result.content if hasattr(item, "text"))
