---
title: Janitor
description: Inventory cleanup candidates and apply recoverable plans.
---

# Janitor

Hermes Janitor creates a deterministic inventory of cleanup candidates across
Kanban boards, cards, Git worktrees and branches, logs, caches, and test
artifacts. Scanning never changes state.

```bash
hermes janitor scan
hermes janitor scan --json
hermes janitor scan --plan ~/.hermes/janitor/plan.json
```

Each row includes its resource identity, reason, safety classification,
reversibility, and a state fingerprint. The classifications are:

- `safe`: deterministic policy allows the proposed action.
- `needs_review`: the resource may be stale, but ownership or intent is not
  proven.
- `must_preserve`: active, dirty, unmerged, default, or otherwise protected.
- `info`: inventory only; no action is authorized.

## Applying a plan

Application requires an explicit saved plan and confirmation:

```bash
hermes janitor apply --plan ~/.hermes/janitor/plan.json --yes
```

Immediately before mutation, Janitor resolves the resource again and compares
its current fingerprint with the saved plan. Changed state is rejected and
must be rescanned. Missing resources are treated idempotently. Board archive
and restore additionally require the Hermes gateway to be stopped; an
out-of-board lifecycle lock drains existing board connections and prevents new
ones during the rename. An archive tombstone keeps the old slug from being
silently recreated until the board is restored.

The initial policy automatically applies only recoverable archives of completed
boards whose slug identifies them as disposable (`factory-canary-*`,
`canary-*`, or `test-*`). The default board is never archiveable. Named boards
with active cards, runs, workflows, or notification subscriptions are
protected, while empty or all-terminal non-canary boards require review.

Irreversible worktree-metadata pruning is separately gated by
`--allow-nonrecoverable`. Worktree directory removal, branch deletion, card
archival, cache deletion, log deletion, and test-artifact deletion are
report-only until a narrower ownership contract authorizes them.

## Restoring a board

Board archives move intact into
`~/.hermes/kanban/boards/_archived/<slug>-<timestamp>/`. Restore one with:

```bash
hermes janitor restore-board \
  ~/.hermes/kanban/boards/_archived/factory-canary-demo-1234567890 \
  --slug factory-canary-demo \
  --yes
```

Restoration accepts only a board directory inside Hermes' archive root and
refuses to overwrite an existing board.

## Scheduled monitoring

Use `hermes janitor scan --json --actionable-only` as a cron monitor script.
Its output is stable: unchanged state produces identical bytes, so monitor mode
can suppress model calls. Give a model only the `needs_review` rows to explain;
do not give it deletion authority. A new scan and explicit deterministic plan
remain the mutation boundary.
