# Backlog & product-ownership process

How datacrystal runs its backlog. This is project-specific product-ops knowledge — it used to live
in `CLAUDE.md`/`AGENTS.md`, but per the memory-file best-practice (keep the every-session file to
facts an agent needs *every* session) it lives here, with a pointer from those files. Invoke the
**Gandalf** skill for any actual prioritization, splitting/merging, refinement, or hygiene question;
this doc is the datacrystal-specific conventions Gandalf applies.

## Sources of authority

- **GitHub Issues are the operational backlog.** `docs/design/ROADMAP.md` stays scope authority
  (in/out) and `docs/design/VISION.md` the product "why". Each roadmap-derived issue cites its
  ROADMAP item in the body.
- **Gandalf (the PO skill) owns prioritization, splitting/merging, refinement, and hygiene.** Sizing
  unit = "concerns"; priority = the Gandalf Score.

## Three orthogonal axes — one tool each (don't fuse them)

- *When it ships* → the **milestone** = one shippable initiative. Historically one sprint wave
  (`Sprint N`); since 2026-07-12 (#168) a multi-wave epic gets ONE **campaign milestone** spanning
  its waves (wave-level issues cut up front for overview; it still closes when the campaign ships;
  one milestone per issue; unscheduled backlog has NO milestone).
- *Which product goal it advances* → **`theme:` labels** (many per issue, perpetual, cross-cut
  sprints — a goal never "completes").
- *The why* → `VISION.md`.

Goals are labels, never milestones (ruled again 2026-07-12) — precisely because a goal spans many
sprints, never closes, and an issue advances several at once.

## Label taxonomy (kept deliberately small — "gandalf-fied")

- **milestone** = initiative (a `Sprint N` wave or a multi-wave campaign; backlog items have none).
- **`priority:`** = the Gandalf band (golden / high / normal / not-now).
- **`theme:`** = product goal.
- **`roadmap`** / **`eval-feedback`** = origin.
- **`epic`** / **`spike`** = Gandalf type.
- **`frozen-api`** = touches the v0.1.0 freeze → v0.2+.
- **`needs-owner-decision`** = blocked on a Sven ruling (no code until answered).
- Plus stock `bug` / `documentation` / `good first issue`.

## Refinement & epics

- **Refinement precedes build-order**: don't pull an issue until it's refined (INVEST + concerns) and
  any `needs-owner-decision` spike is answered. The resulting sequence IS the sprint milestones (the
  live plan, in order); #20 reverse-ref is the standing Golden Ticket. Refined stories + acceptance
  criteria live as a Gandalf comment on each issue.
- **Epics span sprints; materialize sub-stories just-in-time.** A one-wave `epic` is milestoned to the
  sprint where its work *starts*; an epic too big for one wave gets its own **campaign milestone** with
  wave-level issues cut up front (the overview unit — precedent: #168, milestone "Permissions"). Leaf
  stories live as checklists (epic refinement comment / wave issue bodies) and are cut into their own
  issue only when actually pulled — never bulk-create leaf sub-issues ahead of need.

## Sprint token accounting

- **Every sprint records its token spend, and the sprint PR cites it** — planning *and* development —
  as a one-line `Token ledger:` in the PR body, so the cost of agent-driven delivery is in the open
  and pitchable. Numbers are **output-token counts** (the comparable figure across runs), never
  wall-clock.
- **Where the numbers come from:** *planning* = the planning `Workflow`'s reported `subagent_tokens`
  (in the task-completion notification) **plus** the planning session's `/cost`; *development* = each
  implementation session's `/cost` **plus** any implementation `Workflow`'s `subagent_tokens`.
  (`npx ccusage@latest session` reads the local transcripts for a per-session breakdown.)
- **Ledger format** (in the PR body): `Token ledger: planning ≈ N tok · development ≈ M tok (K
  agents, T tool calls)`. Capture the planning figure when planning closes; append the development
  figure at PR-open time.
