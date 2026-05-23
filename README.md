# desktop-act

> Computer-use primitives + multi-desktop pool + autonomous `act()` loop for
> Claude Code. One MCP server, no API key.

`desktop-act` lets Claude (or any MCP client) drive an X11 desktop:
screenshot it, click, type, scroll, launch apps, run a goal-driven loop. It
ships a pooled VNC backend so you can spin up isolated desktops on demand, each
streamed over noVNC for live viewing.

Auth path is **Claude CLI OAuth via `claude-agent-sdk`** — costs ride on a
Claude Max subscription, no `ANTHROPIC_API_KEY` needed.

---

## Install

### As a Claude Code plugin

```text
/plugin marketplace add Lifecycle-Innovations-Limited/desktop-act
/plugin install desktop-act@desktop-act
```

### Via the claude-ops launcher

If you already have [`claude-ops`](https://github.com/Lifecycle-Innovations-Limited/claude-ops),
its bundled `desktop-act-launcher.py` auto-clones this repo into your per-user
cache (`$XDG_CACHE_HOME/desktop-act-mcp` on Linux) the first time the
`/ops:desktop` skill fires.

### Manual

```bash
git clone https://github.com/Lifecycle-Innovations-Limited/desktop-act.git
# Point your MCP host at desktop-act/mcp-server/run.sh (stdio transport)
```

The first invocation auto-bootstraps a Python venv at `${CLAUDE_PLUGIN_ROOT}/.venv`
(or `$DESKTOP_ACT_VENV` if set) from `requirements.txt`.

### System dependencies

You need an X11 stack and a VNC server reachable as `Xvnc` (TigerVNC):

```bash
# Amazon Linux 2023 / RHEL / Fedora
sudo dnf install -y tigervnc-server websockify metacity xterm python3

# Ubuntu / Debian
sudo apt install -y tigervnc-standalone-server websockify metacity xterm python3-venv
```

Optional but recommended: `openbox`, `xdotool`, `firefox`, `nautilus`,
`libreoffice` (whatever GUI apps you want to drive).

---

## Tools exposed by the MCP server

| Tool                | Purpose                                                   |
|---------------------|-----------------------------------------------------------|
| `acquire_desktop`   | Spawn a fresh Xvnc + websockify on the next free display  |
| `release_desktop`   | Kill that desktop's processes, free ports                 |
| `list_desktops`     | Inspect the pool                                          |
| `screenshot`        | Grab the X11 desktop (JPEG default, SHA-deduped)          |
| `observe`           | Screenshot + window list in one round-trip                |
| `list_windows`      | Enumerate top-level X11 windows + geometry                |
| `launch_app`        | Spawn a GUI app on the display                            |
| `click`             | Absolute-coord mouse click (buttons 1–5)                  |
| `keypress`          | Press a key chord (e.g. `Return`, `Control+L`)            |
| `type_text`         | Type a string into the focused window                     |
| `scroll`            | Scroll wheel up/down                                      |
| `batch`             | Execute many primitives in one MCP round-trip             |
| `act`               | Autonomous goal-driven loop via `claude-agent-sdk`        |
| `act_step`          | Single-step variant for transparent in-prompt control     |
| `status`            | Pool + binary inventory                                   |

A `/desktop:act <goal>` slash command is also registered.

---

## Configuration

All knobs are environment variables — no config file required.

| Env var                          | Default                         | Meaning                                 |
|----------------------------------|---------------------------------|-----------------------------------------|
| `CLAUDE_PLUGIN_ROOT`             | (set by Claude Code)            | Plugin root for logs + venv             |
| `DESKTOP_ACT_DISPLAY`            | `:1`                            | Default display when no `session_id`    |
| `DESKTOP_ACT_DISPLAY_MIN`/`MAX`  | `50` / `99`                     | Pool display number range               |
| `DESKTOP_ACT_VNC_PORT_BASE`      | `5900`                          | Pool VNC port base (display N → 5900+N) |
| `DESKTOP_ACT_NOVNC_PORT_BASE`    | `6082`                          | noVNC websockify port base              |
| `DESKTOP_ACT_GEOMETRY`           | `1280x800`                      | Default geometry for new desktops       |
| `DESKTOP_ACT_TMP`                | `/tmp`                          | Where pool state + screenshots live     |
| `DESKTOP_ACT_LOG_DIR`            | `$CLAUDE_PLUGIN_ROOT/logs`      | Where session logs go                   |
| `DESKTOP_ACT_VENV`               | `$CLAUDE_PLUGIN_ROOT/.venv`     | Venv location                           |
| `DESKTOP_ACT_PYTHON`             | `python3`                       | Bootstrap interpreter                   |
| `CLAUDE_CLI_PATH`                | `which claude`                  | Path to `claude` CLI for `act()`        |

---

## Usage patterns

### Streaming primitives (recommended)

Let the parent session drive the loop — call primitives, Read screenshots,
decide, repeat. Every action is visible in the chat transcript.

```text
mcp__desktop-act__acquire_desktop()                  # → session_id
mcp__desktop-act__launch_app(session_id, "firefox")
mcp__desktop-act__screenshot(session_id)             # → /tmp/desktop-act-shots/…jpg
mcp__desktop-act__click(session_id, 640, 400)
mcp__desktop-act__release_desktop(session_id)        # always, even on errors
```

### Autonomous goal loop

```text
/desktop:act open firefox, navigate to example.com, and screenshot the page
```

`act()` spawns a sub-session via `claude-agent-sdk`. Use this when you want
hands-off; otherwise streaming primitives give better transparency.

### Watching live

Every desktop session returns a `novnc_url`. Open it in any browser:

```
http://<box-host>:6082   # first pool desktop
http://<box-host>:6083   # second, etc.
```

---

## Architecture notes

- **Persistent X11 connections.** One `Xlib.Display` per display name, cached
  for the life of the MCP process. Saves the connection-establishment hit per
  primitive call.
- **File-locked pool.** Pool state lives at `/tmp/desktop-act-pool.json`,
  guarded by `flock` on `/tmp/desktop-act-pool.lock`. Safe across
  concurrent agents.
- **SHA-deduped screenshots.** Identical frames return the same path without
  re-encoding. Keeps token/byte cost down across tight loops.
- **JPEG by default.** Switch to PNG with `fmt="png"` when you need lossless.

---

## Project layout

```
desktop-act/
├── .claude-plugin/plugin.json     # plugin manifest (registers MCP server)
├── commands/act.md                # /desktop:act slash command
├── skills/desktop-act/SKILL.md    # when-to-use skill doc
├── mcp-server/
│   ├── run.sh                     # launcher (auto-bootstraps venv)
│   └── server.py                  # FastMCP server (single file)
└── requirements.txt
```

---

## License

MIT — see [LICENSE](./LICENSE).
