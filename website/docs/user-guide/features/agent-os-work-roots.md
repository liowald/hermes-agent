# Agent OS work roots

An Agent OS work root is one durable Kanban task that represents a human request from intake through delivery. It is not a worker task. Hermes keeps the root stable while separate, capability-bounded phase tasks implement, review, fix, and deliver the work.

## Observable contract

For a native Buzz channel thread:

1. One Buzz root creates at most one work root.
2. Hermes reports acceptance only after it has durably created the root and subscribed the same Buzz thread to it.
3. Specification updates the same root and leaves it in triage. The dispatcher cannot run it.
4. Starting work atomically freezes the approved plan and creates exactly one implementation phase.
5. The guarded factory runs implementation, two independent exact-candidate reviews, an optional fixer cycle, and authorized delivery.
6. Routine phase changes stay silent. A genuine factory block wakes L1 with an operator decision request.
7. Hermes reports completion only after the root contains a verified factory receipt.

The receipt exposed at intake contains the root id, current stage, active phase id when one exists, and the next gate. A heartbeat or live process is liveness evidence only; it is never completion evidence.

## Source of truth

The Kanban database is the only lifecycle authority. Buzz is an input and receipt surface. The root task projects the current stage through `current_step_key`, while `factory_workflows.state` remains authoritative after factory adoption.

Managed roots use `workflow_template_id=guarded-work-v1`. Their lifecycle is:

```text
intake -> planned -> implementing -> reviewing -> fixing
                                      |             |
                                      +----------> reviewing
                                      |
                                      +----------> delivering -> completing -> done
```

Any active phase may enter blocked and later resume through the deterministic
factory retry contract.

The approved title and body are serialized into `hermes.work-root.plan.v1` at adoption. Hermes stores its SHA-256 digest with the factory workflow and carries the digest through implementation, review, repair, delivery, and the final receipt. Editing the root plan after adoption is rejected.
Buzz L1 may submit the approved title and plan in the adoption call; Hermes persists that update and creates the implementation phase in one transaction.

## Buzz threading and idempotency

For channel messages, the marked Nostr `root` event is the work-thread id. A new top-level message uses its own event id. Replies retain the root id and use their own event id as the immediate reply target. Direct-message routing remains unchanged.

Hermes derives the request key from the Buzz channel and work-thread ids. A caller-supplied key must match that canonical key or Hermes rejects it. Re-delivery through WebSocket, polling, a gateway restart, or an L1 retry therefore resolves to the existing root instead of creating duplicate work. Adoption also verifies that the supplied root belongs to the current Buzz thread before subscribing or starting a worker.

The originating subscription uses `notify+wake` and stores the exact channel and thread. Creation without that required subscription is not an accepted request; L1 receives an actionable error containing the already-created root id and may safely retry.
For Buzz direct-create and adoption calls, Hermes establishes this subscription before it activates any implementation phase. A subscription failure leaves the root held in `intake` with no worker running.

## Boundaries

This contract deliberately does not add a general workflow engine, a second task database, a new dashboard, an ACP bridge, a Codex Cloud worker lane, or a taxonomy of specialist agents. The existing Kanban task, factory state machine, profile capabilities, event log, runs, comments, attachments, and notification subscriptions are the narrow waist.

Codex Cloud remains a later lane. Its current experimental CLI has no submission idempotency key or cancellation command, so a lost response can otherwise create duplicate paid work.

## Recovery

- A root in `intake` or `planned` remains in triage and can be inspected or specified again.
- Repeating intake or adoption with the same request key returns the existing root.
- A blocked factory preserves its root, plan digest, candidate, reviews, and phase receipts. `factory retry` repairs only the invalid phase and never bypasses review or delivery.
- Active factory roots and phase cards cannot be archived or deleted. Completed cards may be archived, but permanent deletion is refused so the factory audit remains intact.
- The factory watchdog reconciles durable phase receipts. It does not infer progress from heartbeats alone.
