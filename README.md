# CorvusPixel

An interactive pixel-art canvas driven from a terminal. **You** draw — mouse
click/drag or arrow keys + space — in a terminal window that `open_canvas` opens.
Claude Code reads what you drew with `see_canvas()` (a compact symbol grid, cheap
in tokens) and only draws itself when you ask. No files, real time.

## Quick start

```bash
./setup.sh
```

This creates a `.venv`, installs `mcp` and `rich`, and registers the server with
`claude mcp add --scope project`. Then, in Claude Code, call the **`open_canvas`**
tool: a full OS terminal window opens running the interactive canvas app,
connected to this session's socket. Draw, then ask Claude to look at it
(`see_canvas`).

| Tool             | Description                                        |
| ---------------- | -------------------------------------------------- |
| `open_canvas`    | Open the interactive canvas window (user draws)    |
| `see_canvas`     | Compact symbol grid + legend — how Claude reads it |
| `set_pixel`      | One pixel: `set_pixel(x, y, "#rrggbb")`            |
| `fill_rect`      | Filled rect (clipped): `fill_rect(x, y, w, h, c)`  |
| `draw_line`      | Line: `draw_line(x1, y1, x2, y2, "#rrggbb")`       |
| `clear`          | Fill whole canvas: `clear("#rrggbb")`              |
| `init_canvas`    | New canvas: `init_canvas(width, height, color)`    |
| `get_canvas`     | Full state as JSON (debugging)                     |

## Drawing in the canvas window

- **Mouse** — click to paint the cell under the cursor; click and drag to paint
  a line. A click paints both halves of the cell (one display cell = two canvas
  rows).
- **Keyboard**
  - arrow keys — move the cursor (reverse-video cell)
  - `space` — paint/toggle the pixel at the cursor with the current color
  - `x` — erase the pixel at the cursor
  - `c` — cycle the palette · `1`–`8` — pick a palette color directly
  - `q` or Ctrl+C — quit the window

Every edit is applied locally for instant feedback and written back to the
server, which rebroadcasts it to every connected window.

## Manual run

If you skip MCP registration:

```bash
# terminal 1 — the MCP server (stdio transport)
.venv/bin/python server.py --socket /tmp/corvuspixel-12345.sock
# terminal 2 — the interactive canvas
.venv/bin/python canvas_app.py --socket /tmp/corvuspixel-12345.sock
```

## How it works

1. **`server.py`** — the MCP server (stdio transport). It holds the 2-D RGB
   canvas, which is the single source of truth. Each window connection gets a
   full snapshot; every change (from any window, or from a Claude draw tool) is
   pushed as an `update` carrying only the changed cells.
2. **`canvas_app.py`** — the interactive window. Connects to the server's socket
   (retrying forever), renders the canvas with diff-only truecolor `▀`
   half-blocks, and turns keyboard/mouse input into pixel edits that are applied
   locally and sent back to the server as `{"type": "edit", ...}`. The server
   applies them and rebroadcasts.
3. **Session-scoped socket** — the socket path is derived from the server's
   parent PID (the Claude Code session that spawned it), e.g.
   `/tmp/corvuspixel-<ppid>.sock`. A window opened from this session always talks
   to exactly this server instance, never a stale one from another session.
   `CORVUSPIXEL_SOCK` overrides it.
4. **`setup.sh`** — the one-command setup (venv, deps, MCP registration). It no
   longer manages tmux; `open_canvas` spawns the window. `open_canvas` launches
   a terminal via, in order, Windows Terminal (`wt.exe`), Terminal.app
   (`open -a Terminal`), then `x-terminal-emulator`, `gnome-terminal`,
   `konsole`, or `xterm` on Linux.

### Process split tradeoff

The canvas lives in the server process; the window app is a thin client. We chose
this over shared memory because the server must be the single source of truth
anyway (Claude reads the canvas through the server, and multiple windows could
connect). The app applies each edit locally before sending it, so the user sees
the stroke immediately; the socket write-back is the only latency, and a Unix
socket round-trip is microseconds. The cost of the split is that the app cannot
draw while disconnected — its edits are dropped until it reconnects (it always
reconnects, and each reconnect re-syncs from a fresh full snapshot).

### Rendering

Each display cell is the `▀` character: foreground color = top canvas pixel,
background color = the pixel below it, so one terminal row shows two canvas
rows. rich's `Live` context manages terminal lifecycle; the cell diffing is our
own, because stock `Live` redraws its whole region on every refresh (flicker).
Input is parsed directly from stdin (arrow-key CSI sequences and SGR mouse
reports) rather than pulling in a widget framework — the diff-only renderer is
the core requirement and a framework would fight it.

## Requirements

- Python 3.10+
- A truecolor (24-bit) terminal — older terminals show wrong colors
- Terminal mouse support (nearly universal; SGR mouse mode is enabled by the app)
- The `claude` CLI (for `setup.sh` registration only); `open_canvas` needs one of
  the terminals listed above

## Notes

- `init_canvas` resets the canvas — user-drawn pixels are lost, as expected.
- Coordinates are 0-based with the origin top-left. `draw_line` and `fill_rect`
  clip to the canvas; `set_pixel` out of bounds returns an error.
- `see_canvas()` downsamples to a ≤16×16 symbol grid with a color legend, so it
  stays cheap in tokens on any canvas size.
- Tests: `.venv/bin/python -m pytest`
