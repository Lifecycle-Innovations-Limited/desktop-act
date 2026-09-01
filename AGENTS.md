# Repo-level agent instructions

Machine-level conventions still apply: `~/AGENTS.md`.

## Project-specific commands

| Command | How to run |
|---------|-----------|
| Install deps | TODO: e.g. `npm install` / `uv sync` / `pnpm install` |
| Build | TODO: e.g. `npm run build` / `pnpm build` |
| Test | TODO: e.g. `npm test` / `pytest` |
| Lint | TODO: e.g. `npm run lint` / `ruff check .` |
| Type check | TODO: e.g. `npm run typecheck` / `tsc --noEmit` |

## Workspace safety

Before mutating this checkout, prefer to acquire the workspace lock:

```bash
agent-workspace-lock hold "$(pwd)"
```

Release when done:

```bash
agent-workspace-lock release "$(pwd)"
```

## Notes

- Default branch: TODO
- Required env files: TODO
- Required services (Docker): TODO
