@AGENTS.md

Version `0.9.0` (single-sourced from `pyproject.toml`; `release.yml` bumps every mirror — never by
hand). The engineering guide above is vendor-neutral and lives in `AGENTS.md`; the notes below are
Claude Code idiom that other agents don't share.

## Claude Code

- **Gandalf (the PO skill) owns the backlog** — invoke it for any prioritization, story
  splitting/merging, refinement, sizing, or hygiene question. The datacrystal-specific conventions it
  applies (the milestone/theme/why axes, the label taxonomy, epic materialization) are written down in
  `docs/design/BACKLOG-PROCESS.md`.
- **Sprint token ledger**: every sprint PR cites its output-token spend (planning + development) as a
  one-line `Token ledger:` in the body — the cost of agent-driven delivery, in the open. Format and
  where the numbers come from (`/cost`, a Workflow's `subagent_tokens`, `ccusage`) are in
  `docs/design/BACKLOG-PROCESS.md`.
- **Auto-memory**: the persistent memory index is `MEMORY.md`; write project/feedback/user facts there
  as they're learned (the harness injects the recall + write contract each session).
