# Agent Kanban Board — design spec

Handoff brief. This captures design decisions already made; it is not a request to
redesign. Ask before deviating from anything under **Invariants**.

---

## 1. What this is

A kanban board whose workers are AI agents rather than people. It serves three roles:

1. **Dispatch queue** — agents pull work from it
2. **Review gate** — human approves agent output before it lands
3. **Fleet monitor** — shows who is running what

All three are views over one shared state. Build them in the order listed above.

## 2. Environment

- Multiple agent runtimes on a LAN, not one process: a Raspberry Pi (always on) running
  a Hermes agent, a Windows host serving local Ollama models, and Claude Code.
- Agents reach each other over SSH; the Pi is the always-on coordinator.
- A file-backed vault on the Pi already serves as the agent's persistent memory. The
  board should live in the same style — plain files, git-tracked.
- Claude Code fork-subagent mode with git worktrees is in use for parallel execution.

**Implication:** the board must be readable and writable by heterogeneous processes on
different machines with no shared runtime. Plain files + git are the integration layer.
Do not introduce a server, database, or message broker in the first milestone.

## 3. Decisions already made

| Decision | Rationale |
|---|---|
| Cards come from both a human and a planner agent | Planner decomposes goals; human writes one-offs |
| Planner output lands in `inbox`, never `ready` | A planner emitting 30 cards must not flood executors. Human promotion is the WIP valve. |
| One file per card | Concurrent writers from multiple machines; a single board file gives last-write-wins corruption |
| Status is a field, not a directory | Moving files between column directories breaks git history and inbound links |
| Leases with TTL + heartbeat | A crashed agent must release its card automatically |
| `done` and `merged` are distinct | An agent's "done" is a PR, not a landed change |

## 4. Card lifecycle

```
  human ──┐
          ├──> inbox ──> ready ──> claimed ──┬──> needs_input ──> ready
planner ──┘                  ^               │
                             │               └──> needs_review ──┬──> merged
                             └───────────────────────────────────┘
                                        (rejection returns to ready)
```

States:

- `inbox` — proposed, not yet executable. Only a human moves a card out of here.
- `ready` — claimable by any agent.
- `claimed` — an agent holds a live lease and is working.
- `needs_input` — agent hit an ambiguity and wrote a question into the card. Returns to
  `ready` once answered.
- `needs_review` — work complete, awaiting human approval. Approve → `merged`,
  reject → `ready` with feedback appended.
- `merged` — change has actually landed.

## 5. Card schema

One file per card at `cards/<id>.md`, YAML frontmatter plus a free-text body.

```yaml
---
id: "042"
title: short human-readable name
status: claimed          # inbox|ready|claimed|needs_input|needs_review|merged
origin: planner          # planner|human
parent: goal-7           # present only for decomposed cards
depends_on: ["038"]
spec:
  goal: one paragraph, executable by an agent that has seen no prior conversation
  acceptance:
    - testable condition
    - testable condition
  scope_allow: ["src/board/**"]
  scope_deny: ["src/auth/**", ".env*"]
runtime: claude-code     # claude-code|hermes|ollama-local
tier: act                # plan|act
lease:
  owner: hermes@pi
  claimed_at: 2026-09-12T08:44:00Z
  expires_at: 2026-09-12T09:14:00Z
  heartbeat_at: 2026-09-12T08:58:00Z
budget:
  max_usd: 0.50
  spent_usd: 0.12
artifacts:
  worktree: ../wt/042
  branch: card/042
  transcript: runs/042.jsonl
  pr: null
question: null           # set when status == needs_input
---

Free-text notes, review feedback, and agent progress log append here.
```

Rule of thumb for `spec.goal`: if a fresh agent with no conversation history cannot
execute the card, the card is underspecified and belongs in `inbox`.

## 6. Invariants

These are the correctness properties. Violating any of them makes the board unsafe for
concurrent agents.

1. **Claiming is atomic.** Use `O_EXCL` creation of `cards/<id>.lock` or a rename-based
   claim. Read-modify-write on the card file is not sufficient.
2. **No card is claimed by two owners.** A claim must fail loudly, not silently
   overwrite.
3. **Leases expire.** A sweeper reaps cards whose `expires_at` has passed and whose
   `heartbeat_at` is stale, returning them to `ready` with an incident note appended.
   Agents refresh `heartbeat_at` periodically while working.
4. **Only a human promotes `inbox` → `ready`.** No agent may perform this transition,
   including the planner that created the card.
5. **Only a human performs `needs_review` → `merged`.** Rejection is
   `needs_review` → `ready` with feedback appended to the body.
6. **Agents respect `scope_deny`.** A card that needs to touch a denied path goes to
   `needs_input`, not ahead anyway.
7. **Every state transition appends to the card body** with timestamp and actor. The
   card is its own audit log.

## 7. Build order

**Milestone 1 — dispatch.** Card schema, directory layout, claim/release library,
lease sweeper, and a CLI: `card new`, `card claim`, `card release`, `card list`.
Enough that one agent can pull and complete a card end to end.

**Milestone 2 — review gate.** `needs_review` and `needs_input` handling, plus a
low-friction approve/reject path. One keystroke per card is the target; a human
reviewing agent output is the bottleneck in this whole system.

**Milestone 3 — monitor.** A read-only render of the card directory — board columns,
live leases, stale-heartbeat warnings, spend per card. Static HTML regenerated on a
timer and served from the Pi is sufficient. This falls out nearly free once the files
exist; do not build it first.

**Milestone 4 — planner.** An agent that decomposes a goal into `inbox` cards. Deferred
deliberately: the card schema needs to prove itself against hand-written cards before a
generator targets it.

## 8. Non-goals for now

- Web UI with drag-and-drop. Status changes happen via CLI and file edits.
- Auth, multi-user, or anything networked beyond the LAN.
- Auto-promotion of planner cards. Revisit only after the planner has a track record.
- Real-time push. Polling is fine at this scale.

## 9. Open questions

- One card ↔ one git worktree: clean mapping, but what happens to the worktree on
  rejection — reuse or recreate?
- WIP limits here are about token spend, rate limits, and merge conflicts rather than
  cognitive load. Should the limit be per-runtime, per-budget, or per-touched-path?
- Does `depends_on` gate claiming (card stays unclaimable until deps merge), or is it
  advisory?
