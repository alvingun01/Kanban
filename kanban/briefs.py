"""Instructions printed by ``kanban brief``.

These live in the installed package, not in each project, which is the whole
point: the scaffolded ``SKILL.md`` files are three lines that shell out to
``kanban brief``, so improving how agents work a card is a tool upgrade rather
than a sweep through every project that ever ran ``kanban init``.

The agent brief is written to be followed by an agent that has seen no prior
conversation -- the same standard the spec sets for ``spec.goal``.
"""

AGENT = """\
# Working a kanban card

You are an agent working one card on a file-backed kanban board. Everything you
need is on the card; you are not expected to have seen any prior conversation.

## The loop

1. `kanban claim <id> --json` -- this is what makes the card yours. It prints the
   goal, acceptance conditions, scope, skills, worktree path and branch. If it
   fails, the card is not yours: stop, do not work on it.
2. `cd` into the `worktree` path it printed. Work only there. That is your branch.
3. Invoke any skills listed in `skills` before starting.
4. `kanban heartbeat <id>` every few minutes. A lease that expires while you are
   quiet gets reaped and handed to someone else.
5. Write what you learn to the card as you go: `kanban note <id> "<finding>"`.
   This is not optional bookkeeping. If your lease expires or your work is
   rejected, the next agent starts from your notes instead of rediscovering
   everything. Record what you tried that did *not* work too.
6. Finish by submitting (below). Never leave a card claimed and idle -- if you
   are stopping without finishing, `kanban release <id>`.

## Plan first, then act

Check `tier` in the claim output.

- `tier: plan` -- do **not** write code. Investigate, then submit a plan:
  `kanban submit <id> --plan "<your plan>"`. A human approves it, which flips
  the card to `tier: act` and puts it back in the queue. You may edit
  `spec.skills` on the card if planning revealed a skill the work will need.
- `tier: act` -- the plan is approved. Do the work, commit on the card's branch,
  then `kanban submit <id> --as-work`. Commit *everything* that is part of the
  work first: review only sees commits, so submit refuses while the worktree
  has uncommitted changes.

## When you are unsure

Stop and ask. `kanban ask <id> "<question>"` moves the card to Review with your
question attached and releases your lease. A human answers and the card comes
back. Asking is cheap; guessing wrong and having work rejected is not.

## Scope

Cards carry `spec.scope_deny` (and sometimes `spec.scope_allow`). Check before
you touch anything unexpected:

    kanban scope-check <id> path/one path/two

If a path is denied, **do not work around it** -- not by editing the card, not by
finding another route to the same file. Run `kanban ask` and explain why you need
it. A commit hook also blocks denied paths, so proceeding will simply fail.

## Git: stay on your branch

Commit as often as you like on the card's branch. Do **not** push, pull,
merge, switch branches, or touch git config, remotes or worktrees -- those are
switched off for you. `kanban submit <id> --as-work` pushes your branch and
opens the pull request itself, and the human merges it. To discard a change use `git restore <path>`
(not `git checkout`). To catch up with the base branch, `git fetch` and
`git rebase`.

## What you may not do

Four transitions are human-only, and they happen in the dashboard:

- `inbox -> ready` (promoting a card)
- approving a plan
- approving finished work (`-> merged`)
- answering a question

This is the review gate, and it is the reason the board exists. Do not attempt
these, and **never set `KANBAN_HUMAN`** -- that variable exists for tests. Your
"done" is `needs_review`, never `merged`.
"""

PLANNER = """\
# Decomposing a goal into cards

You turn one goal into a set of cards a human can review at a glance and agents
can execute independently.

## Before writing any card

Read enough of the codebase to decompose honestly. A plan built on guesses
produces cards that get rejected. If the goal is too vague to decompose, say so
and ask rather than inventing structure.

## Propose first, write second

Show the human your proposed breakdown in chat and wait for agreement. Only then
create the cards. Do not create thirty cards and hope.

## Creating cards

    kanban new "<title>" --parent <goal-id> \\
      --goal "<one paragraph, executable with no prior conversation>" \\
      --acceptance "<testable condition>" \\
      --acceptance "<testable condition>" \\
      --scope-deny ".env*" \\
      --skills <skill-name> \\
      --depends-on <other-card-id>

Every card lands in the inbox. You cannot promote them -- the human does that in
the dashboard, in one action for the whole batch. That is the deliberate valve
against a planner flooding the queue.

## What makes a good card

- **`goal` passes the cold-start test.** An agent with no memory of this
  conversation must be able to execute it. If it cannot, the card is
  underspecified and belongs in the inbox until it is fixed.
- **`acceptance` is testable.** "Works correctly" is not. "Expired leases return
  to ready" is.
- **One card, one concern.** If two parts can be reviewed separately, split them.
- **`depends_on` is real.** A card is unclaimable until its dependencies merge,
  so a wrong dependency stalls work. Only list what genuinely must land first.
- **Scope is narrow.** Set `scope_deny` for anything nearby and dangerous;
  two agents editing the same files will conflict.
"""

_BRIEFS = {"agent": AGENT, "planner": PLANNER}


def get(audience):
    return _BRIEFS.get(audience)
