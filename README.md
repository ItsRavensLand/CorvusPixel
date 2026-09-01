# CorvusPixel

An interactive pixel-art canvas driven from a terminal. **You** draw — mouse
click/drag or arrow keys + space — in a terminal window that `open_canvas` opens.
Claude Code reads what you drew with `see_canvas()` (a compact symbol grid, cheap
in tokens) and only draws itself when you ask. No files, real time.

## Quick start

```bash
./setup.sh   # once: create .venv, install deps, register the MCP server at user scope
```

Run `setup.sh` once from this project. It registers the CorvusPixel MCP server
at **user scope**, so from then on **`/canvas` works from any directory** — you
don't need to `cd` into CorvusPixel first. Just type **`/canvas`** in any
Claude Code session on this machine to open the drawing window. Add an optional
size: `/canvas 64x64` opens a fresh 64×64 canvas.

```text
Claude Code > /canvas
> Canvas opened. Draw, then ask Claude to look at it.
```

`/canvas` never opens a second window: if one is already connected to the
session it just says "Canvas already open in this session."

| Tool             | Description                                        |
| ---------------- | -------------------------------------------------- |
| `canvas_status`  | "open" / "closed" — is a canvas window connected?  |
| `open_canvas`    | Open the interactive canvas window (user draws)    |
| `see_canvas`     | Compact symbol grid + legend — how Claude reads it |
| `set_pixel`      | One pixel: `set_pixel(x, y, "#rrggbb")`            |
| `fill_rect`      | Filled rect (clipped): `fill_rect(x, y, w, h, c)`  |
| `draw_line`      | Line: `draw_line(x1, y1, x2, y2, "#rrggbb")`       |
| `clear`          | Fill whole canvas: `clear("#rrggbb")`              |
| `init_canvas`    | New canvas: `init_canvas(width, height, color)`    |
| `get_canvas`     | Full state as JSON (debugging)                     |

## Drawing in the canvas window

- **Mouse** — click the toolbar above the canvas to activate any tool (paint,
  eraser, brush size, palette, column/row resize, quit); click a slot on the
  brush bar (or drag its `●` handle) to size the brush; click a palette swatch
  or a swatch in the bottom colour row to pick a color (the rainbow swatch at
  the end opens a small hex input — type six hex digits, Enter to pick, Esc to
  cancel); click (or click-drag) on the canvas to paint with the brush. Pick a
  **shape tool** from the top-right corner of the window (filled/hollow
  rectangle or square, or a straight line) and click-drag on the canvas to draw:
  a dimmed preview follows the cursor, release commits the shape in the current
  color, Escape cancels it. The **fill** and **text** tools sit in the same
  top-right corner: with **fill**, one click flood-fills the whole 4-connected
  same-color region under the cursor with the current color; with **text**, a
  click places the insertion point and typing draws 5×7 bitmap characters in the
  current color — the brush-size bar doubles as the **text-size control** while
  this tool is active (each font pixel draws as an N×N block, so a larger scale
  makes chunkier, more readable text; changing it mid-typing only affects
  characters typed after the change) — Backspace erases the last character,
  Enter moves to a new line below, and Escape cancels the whole in-progress
  session (click elsewhere or switch tools to finalize it). The **label** tool
  (same top-right corner) is the literal-text counterpart: click to place a
  caret at a single terminal character cell, type to write real terminal
  characters in the terminal's own font — crisp at any size — right over the
  pixel grid in the current color, Backspace removes the last character, Enter
  starts a new line below, Escape cancels the whole in-progress label, and
  clicking elsewhere or switching tools finalizes it. Every finished label is a
  single selectable object with its own id, position (per line), text and
  color; the **select** tool (`v`) clicks inside a label's box to select it (a
  cyan border wraps the whole thing), click-drags to move it (a dimmed preview
  follows the cursor; release commits the new position), Delete or Backspace
  removes it entirely, and Enter re-opens it for in-place editing — cursor at
  the end of the text, Backspace/typing modify it in place, Escape reverts to
  the pre-edit content (it never deletes). Only one label is selected at a
  time, and selection never touches the pixel grid. Labels show up in
  `see_canvas`.
- **Keyboard**
  - A live reminder of all these shortcuts sits in the bottom-left corner of the
    window, packed into small labelled groups (move / draw / brush / shapes /
    canvas / other); it shrinks on narrow windows and disappears when there is
    no room.
  - arrow keys — move the cursor (a blinking reverse-video block framed by
    `[ ]` brackets, so it stays visible even on an empty canvas)
  - `space` — paint the pixels at the cursor with the current color
    (idempotent: painting the same pixel twice never toggles it back)
  - `x` — erase at the cursor
  - `e` — toggle the eraser tool (paints the background color)
  - `r`/`o` — filled / hollow rectangle tool ·
    `f`/`s` — filled / hollow square tool ·
    `l` — straight-line tool (each draws on a click-drag; outline and line
    thickness follow the brush size) · `p` — back to the paint tool
  - `b` — bucket fill: one click flood-fills the connected same-color region ·
    `t` — text tool: click to place the insertion point, type to draw,
    Backspace erases, Enter moves to a new line, Escape cancels the session;
    while this tool is active the brush bar / `+` `-` set the text scale ·
    `a` — label tool: click to place a terminal-cell caret, type literal
    terminal characters right over the pixels, Backspace removes the last one,
    Enter drops a line, Escape cancels the whole in-progress label ·
    `v` — select tool: click inside a label to select it (a cyan border wraps
    its box), drag to move it, Delete/Backspace removes it, Enter re-opens it
    for in-place editing (Escape reverts)
  - `+`/`-` — grow/shrink the square brush (1×1 … 7×7, centred on the cursor;
    the toolbar's brush bar does the same with the mouse). With the **text tool
    active** they set the text scale instead (each font pixel draws as an N×N
    block; the brush bar doubles as the same control)
  - `[`/`]` — shrink/grow the canvas width (right edge) ·
    `{`/`}` — shrink/grow the height (bottom edge)
  - `c` — cycle the palette · `1`–`8` — pick a palette color directly
  - `Tab` — open the visual palette; arrow keys move the highlight,
    Enter/space confirms
  - `q` or Ctrl+C — quit the window

The brush is a square centred on the cursor, and every paint (keyboard or
mouse) stamps the whole square — a 3×3 brush paints a 3×3 block around where
you aim. Resizing the canvas keeps every pixel that still fits and drops the
rest at the right/bottom edges; the server's copy (and `see_canvas`) follows.

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
   locally and sent back to the server as `{"type": "edit", ...}` (and canvas
   size changes as `{"type": "resize", ...}`). The server applies them and
   rebroadcasts.
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
rows. To make the pixels read as squares, each logical pixel is drawn `CELL_W`
(2) terminal columns wide — a pixel is 2 columns × half a row, which looks
square on the common ~1:2 terminal fonts. The whole canvas is centred in the
terminal — a clickable toolbar (tool buttons + brush-size bar) sits one row
above it, included in the centering math — and everything re-centres when you
resize the window. Below the canvas sit two colour rows: the 8-swatch palette
(arrow keys + Tab, or click) and an always-visible row of common colours
ending in a rainbow "custom colour" swatch that opens a hex input. The toolbar
renders single-width Unicode icons when the terminal supports them
(`CORVUSPIXEL_ICONS=0` forces text labels). The cursor is a blinking
reverse-video block framed by `[ ]` brackets, so it stays visible even on a
blank canvas where reverse video alone would be invisible. rich's `Live`
context manages terminal lifecycle; the cell diffing is our own, because stock
`Live` redraws its whole region on every refresh (flicker). Input is parsed
directly from stdin (arrow-key CSI sequences and SGR mouse reports); the
toolbar buttons and brush bar are small custom clickable regions hit-tested
over those same mouse reports rather than a widget framework — the diff-only
renderer is the core requirement and a framework would fight it.

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
