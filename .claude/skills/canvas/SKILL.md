---
name: canvas
description: Open the CorvusPixel drawing canvas in a new terminal window, linked to this session.
---

# /canvas — open the CorvusPixel drawing canvas

Open the pixel-art canvas in a new terminal window connected to this session's
socket. This is the one command to remember — no tool names needed.

## 1. Confirm the MCP server is registered

The `open_canvas` and `canvas_status` tools come from the CorvusPixel MCP
server, registered at **user scope** — `setup.sh` does this once, and it makes
the tools available in every Claude Code session on this machine, no matter
which directory you launch from. If they are not available in this session, the
server just isn't registered yet (only ever happens before first-time setup).
Tell the user:

> The CorvusPixel tools aren't registered on this machine yet. Run
> `cd /path/to/CorvusPixel && ./setup.sh` once, then restart Claude Code so
> the tools load, and try /canvas again.

Do not call `open_canvas()` until the tools exist.

## 2. Don't open a duplicate window

Call `canvas_status()` first. If it returns `open`, a canvas window is already
connected to this session — do **not** call `open_canvas()` again. Tell the
user: "Canvas already open in this session."

## 3. Open the canvas

- **No size argument**: call `open_canvas()`. The canvas keeps its current
  size (the session default, or whatever `init_canvas` was last given).
- **Size argument** (`/canvas 64x64`): parse `$ARGUMENTS` as `WIDTHxHEIGHT`
  (e.g. `64x64`, case-insensitive `x`, surrounding whitespace ignored). Call
  `init_canvas(width=WIDTH, height=HEIGHT)` to (re)create the canvas at that
  size, then call `open_canvas()`.

The window connects to this session's socket automatically; the user draws
there with mouse or keyboard. Read their drawing back with `see_canvas()` only
when it is relevant to the conversation.
