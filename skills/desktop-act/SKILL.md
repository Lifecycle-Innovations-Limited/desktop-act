---
name: desktop-act
description: "Computer-use primitives for the X11 desktop on this dev box. Use when a task needs to drive a GUI app, browser, or interact with anything visual that the parent Claude session needs to see and act on."
---

# desktop-act

This plugin exposes computer-use primitives via the `mcp__desktop-act__*` tools
plus a one-shot `act(goal)` that runs an autonomous loop via OAuth (no API key).

## Architecture

- Parent Claude session is the "brain" — calls primitives, Reads screenshots,
  decides next action, calls next primitive. Natural streaming reasoning.
- MCP server provides primitives: screenshot, click, type_text, keypress,
  scroll, list_windows, launch_app.
- High-level `act(goal)` spawns a sub-session via claude-agent-sdk for hands-off
  goals — but the streaming-friendly path is calling primitives directly.

## Watching the desktop

Connect via noVNC at: http://<box-host>:6081
That bridges to Xvnc :1. Whatever the primitives do is visible there in real time.

## When to use

| Task | Approach |
|------|----------|
| Browser dashboard with auth (Stripe/Vercel/AWS/etc) | Prefer `mcp__kapture__*` (Sam's real Chrome) |
| Local app you need to click/type into | `desktop-act` primitives |
| Generic public site automation | `mcp__playwright__*` |
| Perf/console/Lighthouse | `mcp__chrome-devtools__*` |
| "Just do it" hands-off | `mcp__desktop-act__act(goal)` |

See `~/.claude/skills-library/browser-automation.md` for the full decision tree.
