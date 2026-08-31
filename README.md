# CorvusPixel

Draw pixel art with MCP tool calls and watch it appear **live** in a second
terminal pane. Claude Code (or any MCP client) calls drawing tools on a small
Python MCP server; the server keeps the canvas and pushes only the changed
pixels over a Unix domain socket to a renderer process that paints them with the
`▀` half-block character — one terminal row shows two canvas rows. Truecolor
only, no files.

## Quick start

```bash
./setup.sh
```

This creates a `.venv`, installs `mcp` and `rich`, registers the server with
`claude mcp add --scope project`, and opens a tmux session:

- **left pane** — Claude Code
- **right pane** — the live renderer (a 32×32 smiley is already on screen)

Then ask Claude Code to draw, or call the tools directly:

| Tool             | Description                                        |
| ---------------- | -------------------------------------------------- |
| `init_canvas`    | New canvas: `init_canvas(width, height, color)`    |
| `set_pixel`      | One pixel: `set_pixel(x, y, "#rrggbb")`            |
| `fill_rect`      | Filled rect (clipped): `fill_rect(x, y, w, h, c)`  |
| `draw_line`      | Line: `draw_line(x1, y1, x2, y2, "#rrggbb")`       |
| `clear`          | Fill whole canvas: `clear("#rrggbb")`              |
| `get_canvas`     | Full state as JSON (debugging)                     |

## Manual run

If you prefer not to use tmux:

```bash
# terminal 1
.venv/bin/python server.py
# terminal 2
.venv/bin/python renderer.py
```

## How it works

1. **`server.py`** — the MCP server (stdio transport). It holds a 2-D RGB canvas
   and exposes the six tools above. Each tool call mutates the canvas and pushes
   the exact set of changed pixels as one JSON line over a Unix socket
   (`/tmp/corvuspixel.sock`, or `$CORVUSPIXEL_SOCK`).
2. **`renderer.py`** — connects to that socket (retrying forever), applies the
   diffs, and repaints only the changed cells via ANSI cursor moves. Each cell is
   the `▀` character: its foreground color is the top canvas pixel, its
   background color is the pixel directly below it. rich's `Live` context manages
   the terminal lifecycle; the cell diffing is our own, because stock `Live`
   redraws its whole region on every refresh (which would flicker).
3. **`setup.sh`** — the one-command launcher (venv, deps, MCP registration, tmux).

## Requirements

- Python 3.10+
- A truecolor (24-bit) terminal — older terminals show wrong colors
- `tmux` and the `claude` CLI (for `setup.sh` only)

## Notes

- `init_canvas` resets the canvas, so the smiley disappears — that's expected.
- Coordinates are 0-based with the origin top-left. `draw_line` and `fill_rect`
  clip to the canvas; `set_pixel` out of bounds returns an error.
- `get_canvas()` returns a JSON dump for debugging.
- Tests: `.venv/bin/pip install pytest && .venv/bin/python -m pytest`
