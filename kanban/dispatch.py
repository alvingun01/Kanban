"""Spawning agents when a card enters the Execute column.

Dispatch is event-driven: moving a card into Execute is what starts an agent.
Nothing polls on a timer.

Two things keep this honest:

* **Dispatch is a hint, not an authorization.** The spawned agent's first act is
  ``kanban claim``, so the ``O_EXCL`` lock still arbitrates. A double-dispatch
  produces one worker and one loser, not two workers on one card.
* **Never ``--bare``.** Its own help text says OAuth and the keychain are never
  read and auth is strictly ``ANTHROPIC_API_KEY``, so using it would move card
  execution from the user's subscription onto metered API billing.

The ``claude`` binary is resolved at runtime rather than baked in: the copy that
ships inside the VSCode extension lives in a version-pinned directory that
moves on every extension update.
"""

import datetime
import glob
import os
import signal
import shutil
import subprocess
import sys

from . import transitions
from .model import parse_ts, utcnow

CLAUDE_EXTENSION_GLOB = os.path.join(
    os.path.expanduser("~"),
    ".vscode",
    "extensions",
    "anthropic.claude-code-*",
    "resources",
    "native-binary",
    "claude*",
)

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Permission mode for spawned card agents.
#:
#: ``acceptEdits`` on its own is not enough -- it does not cover Bash, and a
#: headless agent cannot answer a prompt, so the agent's first command
#: (``kanban claim``) is refused and the card never starts. ``dontAsk`` is
#: worse: it means *deny* without prompting. Both were observed in testing.
#:
#: The fix is not a wider mode but an explicit allowlist: :data:`ALLOWED_TOOLS`
#: names exactly the commands a card agent needs. Least privilege, and a card
#: that needs more says so rather than everything getting a blank cheque.
DEFAULT_PERMISSION_MODE = "acceptEdits"

#: Exactly what a card agent may run, passed as ``--allowedTools``.
#:
#: Deliberately narrow: the board verbs and git, plus file tools. An agent that
#: needs to run tests or a build will stall until those commands are added to
#: ``extra_allowed_tools`` in ``.kanban/config.json`` -- a visible, per-board
#: decision instead of an invisible default.
ALLOWED_TOOLS = (
    "Bash(kanban *)",
    "Bash(git *)",
    # On Windows agents often pick PowerShell over Bash; without these the
    # very first `kanban claim` needed approval nobody could give.
    "PowerShell(kanban *)",
    "PowerShell(git *)",
    # Cards list skills in spec.skills; the agent must be able to invoke them.
    "Skill",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
)

#: What a read-only card's agent may run: look, report, and nothing else.
#: Run under ``dontAsk``, which *denies* anything not listed -- verified live:
#: the Write tool and ``git add`` were refused, ``git status`` ran.
#: ponytail: ``kanban *`` still includes init/upgrade; narrow it if that matters.
READ_ONLY_TOOLS = (
    "Bash(kanban *)",
    "PowerShell(kanban *)",
    "Bash(git status*)",
    "Bash(git log*)",
    "Bash(git diff*)",
    "Bash(git show*)",
    "PowerShell(git status*)",
    "PowerShell(git log*)",
    "PowerShell(git diff*)",
    "PowerShell(git show*)",
    "Skill",
    "Read",
    "Glob",
    "Grep",
)


def agent_permissions(card, config):
    """(permission mode, allowed tools) for the agent on this card."""
    extra = tuple(config.get("extra_allowed_tools") or ())
    if card.get("read_only"):
        return "dontAsk", READ_ONLY_TOOLS + extra
    return (config.get("permission_mode") or DEFAULT_PERMISSION_MODE,
            tuple(ALLOWED_TOOLS) + extra)


def resolve_claude_bin(config=None):
    """Find the Claude Code CLI, or None.

    Order: an explicit ``claude_bin`` in config, then ``PATH``, then the newest
    binary bundled with the VSCode extension.
    """
    configured = (config or {}).get("claude_bin")
    if configured and os.path.isfile(configured):
        return configured

    found = shutil.which("claude")
    if found:
        return found

    candidates = [
        path
        for path in glob.glob(CLAUDE_EXTENSION_GLOB)
        if os.path.isfile(path) and not path.endswith(".map")
    ]
    if candidates:
        # Sort by the extension directory name so the newest version wins.
        candidates.sort()
        return candidates[-1]
    return None


def available(config=None):
    return resolve_claude_bin(config) is not None


def kanban_invocation(board=None):
    """How a spawned agent should call this tool, and any env it needs.

    Always plain ``kanban``: either it is on PATH already, or ``bin_env``
    below puts the board-local shim there. Handing an agent a quoted
    interpreter path made it fight shell quoting instead of doing the work.
    """
    if shutil.which("kanban"):
        return "kanban", {}
    if board is not None:
        return "kanban", bin_env(board)
    python = sys.executable or "python"
    return '"%s" -m kanban' % python, {"PYTHONPATH": TOOL_ROOT}


def bin_env(board):
    """Env that puts the board-local ``kanban`` shim on PATH."""
    bin_dir = os.path.join(board.board_dir, "bin")
    return {
        "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": TOOL_ROOT,
    }


def build_prompt(card_id, card, kanban_cmd):
    """The prompt handed to a freshly spawned agent.

    Deliberately short. The durable instructions live in ``kanban brief
    agent``, inside the installed package, so they improve without touching
    any project or any card.
    """
    spec = card.get("spec") or {}
    tier = card.get("tier")
    lines = [
        "You are an autonomous agent working one card on this project's "
        "kanban board.",
        "",
        "Card %s: %s" % (card_id, card.get("title") or ""),
        "Tier: %s" % tier,
        "",
        "Step 1 -- read your operating brief and follow it exactly:",
        "    %s brief agent" % kanban_cmd,
        "",
        "Step 2 -- claim the card. This is what makes it yours, and it prints "
        "the goal, scope, skills, and your worktree path:",
        "    %s claim %s --json" % (kanban_cmd, card_id),
        "",
        "If the claim fails, the card is not yours. Stop immediately.",
        "",
    ]
    if spec.get("goal"):
        lines += ["Goal, for orientation:", "    %s" % spec["goal"], ""]
    if tier == "plan":
        lines += [
            "This card is tier 'plan': investigate and submit a plan for human "
            "approval with `%s submit %s --plan \"...\"`. Do not write "
            "production code yet." % (kanban_cmd, card_id),
            "",
        ]
    elif card.get("read_only"):
        lines += [
            "This card is READ-ONLY: investigate and report, nothing else. Do "
            "not create, edit or delete files, and do not change any external "
            "system -- editing tools are switched off for you. Put your "
            "findings in your final message, then `%s submit %s --as-work`."
            % (kanban_cmd, card_id),
            "",
        ]
    else:
        lines += [
            "This card is tier 'act': the plan is approved. Do the work on the "
            "card's branch, then `%s submit %s --as-work`."
            % (kanban_cmd, card_id),
            "",
        ]
    lines += [
        "Before you finish, record what you learned on the card with "
        "`%s note %s \"...\"` -- including what did not work. If your lease "
        "expires or the work is rejected, the next agent starts from those "
        "notes." % (kanban_cmd, card_id),
        "",
        "Never attempt human-only transitions (promote, approve, answer) and "
        "never set KANBAN_HUMAN.",
    ]
    return "\n".join(lines)


def _spawn_flags():
    """Detach the child so it outlives the dashboard process."""
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        }
    return {"start_new_session": True}


class DispatchError(Exception):
    """The agent could not be started."""


def dispatch(board, card_id):
    """Start an agent on one card. Returns a description of what was started."""
    config = board.read_config()
    binary = resolve_claude_bin(config)
    if not binary:
        raise DispatchError(
            "cannot find the Claude Code CLI.\n"
            "       Set claude_bin in .kanban/config.json, or install the CLI "
            "so `claude` is on PATH."
        )

    card, _ = board.load(card_id)
    if card.get("status") != "ready":
        raise DispatchError(
            "card %s is %s, not ready" % (card_id, card.get("status"))
        )

    blockers = transitions.blocking_deps(board, card)
    if blockers:
        raise DispatchError(
            "card %s is blocked by %s"
            % (card_id, transitions.describe_blockers(blockers))
        )

    from .actor import Actor

    max_attempts = config["max_attempts"]
    if (card.get("attempts") or 0) >= max_attempts:
        transitions.park_for_review(
            board,
            card_id,
            Actor.system(),
            "Work was attempted %d times without finishing. A human should "
            "look before another agent tries." % (card.get("attempts") or 0),
        )
        raise DispatchError(
            "card %s has used all %d attempts and has been parked in Review"
            % (card_id, max_attempts)
        )

    # An agent takes a while to start and claim. Until it does the card still
    # reads `ready`, so every dashboard poll used to spawn another agent --
    # three in five seconds, each counted as a "failure", parking a card whose
    # agents were all still running.
    # ponytail: fixed 5-minute grace; make it configurable if claims get slower.
    if is_starting(card, datetime.datetime.now(datetime.timezone.utc)):
        raise DispatchError("card %s: an agent is already starting" % card_id)

    # A separate counter for agents that die *before* claiming. `attempts`
    # only moves on a successful claim, so without this a card whose agent
    # fails at startup would be respawned forever.
    failures = card.get("dispatch_failures") or 0
    if failures >= max_attempts:
        transitions.park_for_review(
            board,
            card_id,
            Actor.system(),
            "An agent was started %d times but never claimed the card, so "
            "something is failing at launch. Check runs/%s.jsonl."
            % (failures, card_id),
        )
        raise DispatchError(
            "card %s: %d agents failed to start; parked in Review"
            % (card_id, failures)
        )

    card["dispatch_failures"] = failures + 1
    card["dispatched_at"] = utcnow()
    board.save(card_id, card, board.load(card_id)[1])

    kanban_cmd, extra_env = kanban_invocation(board)
    prompt = build_prompt(card_id, card, kanban_cmd)

    cwd = board.root
    artifacts = card.get("artifacts") or {}
    if artifacts.get("worktree"):
        candidate = os.path.join(board.root, artifacts["worktree"])
        if os.path.isdir(candidate):
            cwd = candidate

    mode, tools = agent_permissions(card, config)
    args = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        # Required: `--print` with stream-json refuses to start without it.
        # Omitting it made every spawned agent die on its first line.
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        mode,
        # The allowlist, not the mode, is what lets an agent work.
        "--allowedTools",
        ",".join(tools),
        "--add-dir",
        board.root,
    ]
    if config.get("model"):
        args += ["--model", config["model"]]

    env = dict(os.environ)
    env.update(extra_env)
    env["KANBAN_ACTOR"] = "claude-code:%s" % card_id
    # Belt and braces: an agent must never inherit humanity from the server.
    env.pop("KANBAN_HUMAN", None)

    os.makedirs(board.runs_dir, exist_ok=True)
    transcript = board.run_path(card_id)
    handle = open(transcript, "a", encoding="utf-8", newline="\n")
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **_spawn_flags()
        )
    except OSError as exc:
        handle.close()
        raise DispatchError("could not start agent: %s" % exc)
    finally:
        # The child holds its own duplicate of the file descriptor.
        try:
            handle.close()
        except OSError:
            pass

    # Remember the process so a human can stop it later. The agent needs a few
    # seconds to start before it claims and rewrites the card, so this save
    # cannot race its first write in practice.
    card, body = board.load(card_id)
    card["agent_pid"] = process.pid
    board.save(card_id, card, body)

    return {
        "card": card_id,
        "pid": process.pid,
        "cwd": cwd,
        "transcript": os.path.relpath(transcript, board.root).replace("\\", "/"),
        "binary": binary,
    }


def _is_claude(pid):
    """True only if ``pid`` is currently a Claude Code process.

    PIDs get reused. Without this check, stopping an agent that already
    finished could kill whatever unrelated program now holds its old PID.
    """
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
        ).stdout
    else:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, errors="replace",
        ).stdout
    return "claude" in out.lower()


def stop_agent(board, card_id):
    """Stop the agent started for this card. Returns True if one was stopped."""
    card, _ = board.load(card_id)
    try:
        pid = int(card.get("agent_pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not _is_claude(pid):
        return False
    if os.name == "nt":
        # /T takes the agent's own child processes (its shells) with it.
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
        )
        return result.returncode == 0
    try:
        os.killpg(pid, signal.SIGTERM)  # its own session: see _spawn_flags
        return True
    except OSError:
        return False


#: How long a dispatched agent has to claim its card before it is presumed dead.
START_GRACE_SECONDS = 300


def is_starting(card, now):
    """True while an agent launched for this ``ready`` card may still claim it."""
    started = parse_ts(card.get("dispatched_at"))
    return (
        card.get("status") == "ready"
        and started is not None
        and (now - started).total_seconds() < START_GRACE_SECONDS
    )


def running_count(board, now=None):
    """Agents occupying a slot: claimed cards, plus agents still starting.

    Counting only ``claimed`` let every poll during an agent's startup launch
    more agents for other queued cards, blowing straight through the cap.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return sum(
        1
        for _, card, _ in board.all_cards()
        if card and (card.get("status") == "claimed" or is_starting(card, now))
    )


def pump(board, limit=None):
    """Start agents for queued cards, up to the concurrency cap.

    The cap exists because parallel agents share one rate-limit pool: ten cards
    promoted at once would otherwise launch ten agents against a single
    ceiling. Cards over the cap simply stay in Execute.
    """
    config = board.read_config()
    cap = limit or config["max_concurrent_agents"]
    started = []
    skipped = []

    if not available(config):
        return started, [("-", "agent runner unavailable")]

    running = running_count(board)
    for card_id, card, _ in board.all_cards():
        if card is None or card.get("status") != "ready":
            continue
        if running >= cap:
            skipped.append((card_id, "at concurrency cap (%d)" % cap))
            continue
        if transitions.blocking_deps(board, card):
            continue
        try:
            started.append(dispatch(board, card_id))
            running += 1
        except DispatchError as exc:
            skipped.append((card_id, str(exc)))
    return started, skipped
