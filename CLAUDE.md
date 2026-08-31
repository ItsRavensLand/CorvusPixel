# CorvusPixel

An MCP server behind an interactive pixel-art canvas. **You** draw in a terminal
window opened with the `open_canvas` tool — mouse click/drag or arrow keys +
space — and Claude Code reads what you drew with `see_canvas()` (a compact
symbol grid) whenever it's relevant. Claude draws (`set_pixel`, `fill_rect`,
`draw_line`, `clear`, `init_canvas`) only when you ask. Changes are shared over a
session-scoped Unix socket; no files.

## Build & run

- `./setup.sh` — create the venv, install deps, register the MCP server with
  `claude mcp add`.
- `.venv/bin/python server.py` — run the MCP server standalone (stdio transport).
- In Claude Code, call `open_canvas` to open the interactive canvas window; the
  app connects to this session's socket automatically.

## Rules

1. **Only draw when asked.** Never call `set_pixel`, `fill_rect`, `draw_line`,
   `clear`, or `init_canvas` on your own initiative — every draw call costs
   tokens. Reading with `see_canvas()` when relevant to the conversation is fine.
2. **Commit and push every meaningful change.** After every meaningful change to
   this project — a code edit, a new feature, a bug fix, a doc or config
   update — you MUST stage the changed files, commit with a clear, descriptive
   message, and push to the GitHub remote (`git push`). Review with
   `git status` / `git diff` before committing.
