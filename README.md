# Agent Kanban Board

A kanban board whose workers are AI agents. You move a card into **Execute**, an
agent picks it up, does the work in its own git worktree, and hands it back for
you to approve. Nothing lands without you.

Built from [`agent-kanban-spec.md`](agent-kanban-spec.md). Python 3.8+, standard
library only — no pip install, no server to run, no database.

---

## Install once

```powershell
.\install.ps1
```

That puts a `kanban` shim on your user PATH pointing back at this folder. The
code is never copied into your projects, so a fix here reaches every board at
once. Re-run it if you move this folder; `.\install.ps1 -Uninstall` removes the
shim and leaves every board alone.

## Then, in any project

```powershell
cd C:\some\project
kanban init
```

Double-click **`Dashboard.cmd`**. That is the whole interface.

Inside VSCode, the same thing is available as a task: <kbd>Ctrl+Shift+P</kbd> →
*Run Task* → **Kanban Dashboard**. The dashboard is a localhost page, so
VSCode's Simple Browser will render it in an editor tab.

Review is the bottleneck, so the dashboard is keyboard-driven. Press <kbd>?</kbd>
to show the shortcuts:

| Key | Does |
|---|---|
| <kbd>j</kbd> / <kbd>k</kbd>, <kbd>Enter</kbd> | move between cards, open one |
| <kbd>a</kbd> | approve the open card |
| <kbd>r</kbd>, then <kbd>Ctrl+Enter</kbd> | write a reply or rejection, send it |
| <kbd>n</kbd> | new card |

The tab title shows how many cards are waiting on you, so a background tab
still tells you when to look.

---

## How work flows

```
  you ─────┐
           ├──> Inbox ──> Execute ──> Running ──> Review ──> Done
  planner ─┘               ^                        │
                           └────────────────────────┘
```

A new card starts in **Inbox** at `tier: plan`. Clicking **Plan it** sends it to
Execute and an agent investigates and comes back with a plan. You approve the
plan, the card flips to `tier: act`, and the next agent carries it out. Tick
*"skip planning"* when creating a card and it goes straight to execution.

From **Review**, a card is one of three things:

| The agent says | You get | Approving means |
|---|---|---|
| here is my plan | Approve plan / Reject | back to Execute as `tier: act` |
| here is the work | Approve / Reject | **Done** — the change has landed |
| I have a question | an answer box | back to Execute with your answer |

Rejecting always returns the card to Execute with your feedback appended, and
**keeps the worktree and branch** — review feedback usually means "fix this",
not "start over".

## What agents write back

Agents record findings on the card as they work (`## Context`). If a lease
expires or work is rejected, the next agent starts from those notes instead of
rediscovering everything. Every state change appends to `## Log`, so each card
is its own audit trail.

---

## The rules it actually enforces

The board's job is to be safe for several agents running at once. These are the
properties it defends, and how:

| Property | Mechanism |
|---|---|
| Two agents never work one card | `O_EXCL` creation of `cards/<id>.lock`. One syscall decides the race. Verified with 20 processes racing: exactly one wins. |
| A crashed agent releases its card | Leases have a TTL and a heartbeat. The sweeper reaps a lease only when it has **both** expired *and* gone quiet, then returns the card to Execute with an incident note. |
| A card that keeps failing stops | After `max_attempts` (default 3) it is parked in Review instead of respawning an agent that keeps dying. |
| Only you promote and only you merge | Those transitions exist only on the dashboard's write endpoints. See *Trust boundary*. |
| Agents cannot commit denied paths | A `PreToolUse` hook on `git commit` checks staged files against the card's `scope_deny` and blocks the commit. |
| Every change is recorded | All status writes funnel through one function, which appends the audit line in the same breath. |

## Trust boundary

On one laptop, you and Claude Code are the **same OS user**, so file permissions
cannot tell you apart. The interface does instead:

- **You** act through the browser. Write endpoints require both a session cookie
  (`HttpOnly`, `SameSite=Strict`) and a CSRF token injected into the page.
  Another local page cannot read our HTML, so it cannot forge the header.
- **Agents** act through the CLI, which has neither, so `promote`, `approve`,
  `reject` and `answer` are out of reach.
- The server binds `127.0.0.1` only. Tokens live in memory and are never written
  to disk.

`KANBAN_HUMAN=1` bypasses this, for tests and scripts. **It is the trust
boundary** — never set it anywhere an agent can read it, and never in a skill or
a checked-in settings file. `kanban doctor` warns if it is set.

Be clear-eyed about what this buys: it reliably stops an agent that is following
instructions, and it fails closed. It would not stop an agent that went looking
for the browser's cookie jar. On a single-user machine, nothing enforced
in-process can do better.

### What the scope guard does and does not cover

Documented: a `PreToolUse` hook fires *before* any permission-mode check, so a
deny holds even under `bypassPermissions` or `--dangerously-skip-permissions`,
and it fires for background subagents. That makes it real enforcement, not a
suggestion.

Not covered: it guards `git commit`. An agent can still *write* a denied file
locally — it simply cannot commit it. The card records every blocked attempt, so
you see them at review time.

---

## Billing and rate limits

Agents run as headless `claude -p` processes using your **Claude subscription**
— verified by running one with `ANTHROPIC_API_KEY` unset.

The dispatcher never passes `--bare`. Its own help text says OAuth and the
keychain are never read under that flag and auth is strictly
`ANTHROPIC_API_KEY`, so `--bare` would silently move execution onto metered API
billing.

Two things worth knowing:

- **Parallel agents share one rate-limit pool.** `max_concurrent_agents`
  defaults to **2** for that reason, not because two is special. Cards beyond
  the cap wait in Execute and start as slots free. Tune it from what you observe
  — the dashboard shows running count and turns per card.
- **There is per-spawn overhead.** A trivial prompt still loads a system prompt;
  measured around 39k tokens for a one-word answer. Prefer fewer, larger cards
  over many tiny ones, and set `model` in config to keep routine cards off the
  most expensive model.

The dollar figures on the dashboard come from the agent's own transcript and are
**list-price accounting, not a bill** — on a subscription they are a usage signal
only.

### What a spawned agent is allowed to do

Agents run with `--permission-mode acceptEdits` and an explicit allowlist:

```
Bash(kanban *)   Bash(git *)   PowerShell(kanban *)   PowerShell(git *)
Skill   Read   Edit   Write   Glob   Grep
```

`PowerShell(...)` matters on Windows, where agents often pick PowerShell over
Bash; `Skill` lets an agent invoke the skills a card lists.

That list, not the permission mode, is what lets an agent work. Two things
learned by testing rather than from the docs:

- `acceptEdits` alone does not cover Bash, so without the allowlist an agent
  cannot even run `kanban claim` and the card never starts.
- `dontAsk` means **deny** without prompting, not allow. It fails the same way.

**An agent that needs to run tests or a build will stall**, because `pytest` and
`npm` are not on that list. Add what a board needs to `extra_allowed_tools` in
`.kanban/config.json`:

```json
"extra_allowed_tools": ["Bash(pytest *)", "Bash(npm test*)"]
```

That is deliberately a visible, per-board decision rather than a wide default.
Four independent things stand between an agent and your repository: the
allowlist, its own git worktree, the non-bypassable scope guard, and your
approval before anything is marked done.

Spawned agents also get a board-local `kanban` shim on their PATH
(`.kanban/bin/kanban`). Without it an agent is handed a quoted interpreter path
and has to guess the right quoting for whichever shell it picks — in testing one
burned four turns on that before giving up and reading the card file by hand.

---

## Upgrading

```powershell
kanban upgrade            # this board
kanban upgrade --all      # every board you have ever initialised
kanban upgrade --dry-run  # show the changes, write nothing
```

Nothing scaffolded into your project holds logic. `Dashboard.cmd` is four lines;
the skills defer to `kanban brief`; the hook is a shim. So improving how agents
work a card is a tool change, not a visit to every project.

**Your cards are safe.** Cards live in your project while the tool lives
elsewhere, so updating or deleting the tool cannot touch one. On top of that:

- migrations change **frontmatter only** — the body is copied through
  byte-for-byte, because `## Log`, `## Context` and `## Feedback` are the part no
  migration could reconstruct
- `cards/` is snapshotted to `.kanban/backups/<timestamp>/` before any write
- upgrade refuses on a dirty git tree, so git independently holds the old state
- every card is re-validated afterwards, and **one failure rolls the whole batch
  back**
- no migration deletes or renames a card; a field leaving the schema stops being
  *read*, it is not stripped from your files

`kanban rollback` restores the newest snapshot.

---

## Files

In your project:

```
cards/<id>.md          one card, YAML frontmatter + body. The real state.
cards/<id>.lock        claim token (gitignored)
wt/<id>/               that card's worktree, on branch card/<id> (gitignored)
runs/<id>.jsonl        the agent's transcript (gitignored)
.kanban/config.json    lease TTL, concurrency cap, port, base branch
Dashboard.cmd          double-click this
```

`kanban init` appends to `CLAUDE.md`, `.gitignore` and `.claude/settings.json`
between `>>> kanban` / `<<< kanban` markers and rewrites only that span. It will
not clobber your files, and re-running it is a no-op.

## Commands

The dashboard is the human interface; these exist for agents, scripts, and
diagnosis.

```
kanban init                   set up a board here
kanban doctor                 check this board's setup
kanban check                  validate every card
kanban list [--claimable]     list cards
kanban show <id> [--json]     print one card
kanban serve [--open]         run the dashboard
kanban upgrade / rollback     see Upgrading
kanban brief agent|planner    the instructions agents follow
```

Agent verbs: `claim`, `heartbeat`, `note`, `ask`, `submit`, `release`,
`scope-check`, `sweep`.

Human verbs (`promote`, `approve`, `reject`, `answer`) exist but refuse unless
`KANBAN_HUMAN=1`. Use the dashboard.

## Tests

```powershell
python -m unittest discover tests -v
```

Tests are organised by the property they defend. The claim race spawns 20 real
processes rather than mocking; the migration tests run against committed
`tests/fixtures/board-vN/` boards, starting from the schema as the spec
originally wrote it. Fixtures are added, never edited — that is what stops the
migration chain from rotting.

## Known limits

- **One board per project, one dashboard per board.** Three projects means three
  tabs on three ports. A cross-project roll-up would be a natural addition.
- **Claiming is atomic on one filesystem.** That is the case here. Sharing a
  board across machines over git would need claims funnelled through one host —
  git is not an atomic claim mechanism.
- **Automated dispatch on a subscription is a documented-features,
  undocumented-policy gap.** Subagents, `/loop` and scheduled routines are all
  documented; nothing public addresses high-frequency programmatic dispatch. The
  low concurrency default is partly about staying obviously reasonable.
- **Worktrees need a git repo with at least one commit.** Without one the board
  still works, cards just run without worktrees.
- **Keep boards out of synced folders.** OneDrive and friends can touch files
  mid-write and will happily sync `wt/`. Card writes are atomic, but a local path
  is better.
