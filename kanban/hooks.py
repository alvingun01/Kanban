"""The scope-guard commit hook -- the real enforcement of invariant 6.

The board can tell an agent what it must not touch, but it cannot police
another process's writes. A ``PreToolUse`` hook on ``git commit`` can, and the
docs are explicit that it fires *before* any permission-mode check: a deny
holds even under ``bypassPermissions`` or ``--dangerously-skip-permissions``.
That is what turns "agents respect scope_deny" from a documented hope into
something mechanical.

Contract (verified against the hooks reference):

* stdin receives ``{session_id, cwd, hook_event_name, tool_name, tool_input}``,
  with the command string at ``tool_input.command``
* **exit 2 blocks** and the stderr message is shown to the model; exit 0 does
  not approve, it merely declines to object

The check covers every path a commit could pick up -- staged, unstaged and
untracked -- not only what is staged, because the hook runs before the command
and ``git add -A && git commit`` has staged nothing yet. Commits that still slip
past (another shell, ``git commit-tree``) are caught at review: the dashboard
checks the branch diff against ``scope_deny`` too.

A blocked commit does *not* move the card. The agent's best next step is
usually to undo the offending change and commit the rest, and forcing every
violation into review would strand the lease. The attempt is recorded on the
card either way, so nothing is silent.
"""

import json
import os
import sys

from . import model, scope, store, worktree
from .actor import ENV_CARD  # hooks inherit the agent's environment
from .model import utcnow

ALLOW = 0
BLOCK = 2


def _read_event():
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def card_id_for_cwd(board, cwd):
    """Work out which card owns ``cwd``.

    Primary route is the path itself -- worktrees live at ``wt/<id>``. The
    fallback asks the board which claimed card claims this directory, which
    covers an agent working from the project root.
    """
    normalised = os.path.abspath(cwd).replace("\\", "/").rstrip("/")
    parts = normalised.split("/")
    if "wt" in parts:
        index = len(parts) - 1 - parts[::-1].index("wt")
        if index + 1 < len(parts):
            candidate = parts[index + 1]
            if candidate in board.card_ids():
                return candidate

    for card_id, card, _ in board.all_cards():
        if card is None or card.get("status") != "claimed":
            continue
        tree = (card.get("artifacts") or {}).get("worktree")
        if not tree:
            continue
        resolved = os.path.abspath(os.path.join(board.root, tree))
        if resolved.replace("\\", "/").rstrip("/") == normalised:
            return card_id
    return None


def scope_guard(argv=None):
    """Entry point for ``kanban hook scope-guard``. Returns an exit code."""
    event = _read_event()
    cwd = event.get("cwd") or os.getcwd()

    try:
        board = store.Board.find(cwd)
    except store.BoardError:
        # Not a kanban project: none of our business.
        return ALLOW

    # The cwd names the card when the agent has cd'd into its worktree. When
    # it has not -- `cd wt/042 && git commit` from the project root -- the
    # card dispatch started it on is in the environment instead.
    card_id = card_id_for_cwd(board, cwd)
    if not card_id and os.environ.get(ENV_CARD) in board.card_ids():
        card_id = os.environ[ENV_CARD]
    if not card_id:
        return ALLOW

    try:
        card, body = board.load(card_id)
    except Exception:
        return ALLOW

    allow, deny = scope.card_scope(card)
    if not deny and not allow:
        return ALLOW

    tree = (card.get("artifacts") or {}).get("worktree")
    tree = os.path.join(board.root, tree) if tree else cwd
    changed = worktree.changed_paths(tree if os.path.isdir(tree) else cwd)
    if not changed:
        return ALLOW

    report = scope.check_paths(changed, allow, deny)
    denied = report[scope.DENIED]
    if not denied:
        return ALLOW

    listed = "\n".join(
        "  %s   (matches scope_deny %s)" % (path, pattern)
        for path, pattern in denied
    )
    message = (
        "BLOCKED by the kanban scope guard.\n\n"
        "Card %s must not touch these paths, which are changed in your "
        "worktree:\n%s\n\n"
        "Do not work around this. Undo those changes (`git restore --staged "
        "--worktree <path>`, or delete a new file) and commit the rest, or if "
        "the card genuinely needs them, stop and ask:\n"
        "  kanban ask %s \"<why this card needs that path>\"\n"
        % (card_id, listed, card_id)
    )

    # Record the attempt on the card so it is visible at review time.
    try:
        body = model.append_to_section(
            body,
            model.SECTION_CONTEXT,
            "- %s  scope guard blocked a commit touching: %s"
            % (utcnow(), ", ".join(path for path, _ in denied)),
        )
        board.save(card_id, card, body)
    except Exception:
        pass  # never let bookkeeping stop the block

    sys.stderr.write(message)
    return BLOCK


HANDLERS = {"scope-guard": scope_guard}


def run(name, argv=None):
    handler = HANDLERS.get(name)
    if handler is None:
        sys.stderr.write("unknown hook %r\n" % name)
        return 1
    return handler(argv)
