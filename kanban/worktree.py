"""Per-card git worktrees: one card, one worktree, one branch.

A claim puts the agent in ``wt/<id>`` on branch ``card/<id>``. Rejection
**reuses** both -- review feedback usually means "fix this", so the next
claimant continues the existing history rather than starting over. The
worktree is removed only when the card reaches ``merged``.

Worktrees are optional. A board can be initialised in a folder that is not a
git repository, in which case every function here degrades to a no-op and the
card simply records no worktree. That keeps the board usable for tracking work
that is not a code change.
"""

import os
import subprocess


class GitError(Exception):
    """A git command failed."""


def _run(args, cwd, check=True):
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise GitError(
            "git %s failed: %s"
            % (" ".join(args), (result.stderr or result.stdout).strip())
        )
    return result


def is_repo(root):
    """True when ``root`` sits inside a git work tree."""
    try:
        result = _run(["rev-parse", "--is-inside-work-tree"], root, check=False)
    except (OSError, GitError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def branch_exists(root, branch):
    result = _run(
        ["show-ref", "--verify", "--quiet", "refs/heads/%s" % branch],
        root,
        check=False,
    )
    return result.returncode == 0


def has_commits(root):
    """False for a freshly-initialised repo with no HEAD yet."""
    result = _run(["rev-parse", "--verify", "HEAD"], root, check=False)
    return result.returncode == 0


def list_worktrees(root):
    """Return {path: branch} for every registered worktree."""
    result = _run(["worktree", "list", "--porcelain"], root, check=False)
    if result.returncode != 0:
        return {}
    trees = {}
    path = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = os.path.abspath(line[len("worktree "):].strip())
            trees[path] = None
        elif line.startswith("branch ") and path:
            trees[path] = line[len("branch "):].strip().replace(
                "refs/heads/", ""
            )
    return trees


def ensure(board, card_id):
    """Create or reuse ``wt/<id>`` on ``card/<id>``. Returns (path, branch).

    Returns (None, None) when the project is not a git repository, or has no
    commits yet -- ``git worktree add`` needs a HEAD to branch from.
    """
    root = board.root
    if not is_repo(root):
        return None, None
    if not has_commits(root):
        return None, None

    path = board.worktree_path(card_id)
    branch = board.branch_name(card_id)

    existing = list_worktrees(root)
    if os.path.abspath(path) in existing and os.path.isdir(path):
        return path, existing[os.path.abspath(path)] or branch

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    if branch_exists(root, branch):
        # Rejected card coming back round: keep the branch and its history.
        _run(["worktree", "add", path, branch], root)
    else:
        base = board.read_config().get("base_branch") or "HEAD"
        if not branch_exists(root, base):
            base = "HEAD"
        _run(["worktree", "add", path, "-b", branch, base], root)
    return path, branch


def remove(board, card_id, force=False):
    """Remove a card's worktree, keeping the branch. Safe if absent."""
    root = board.root
    if not is_repo(root):
        return False
    path = board.worktree_path(card_id)
    if os.path.abspath(path) not in list_worktrees(root):
        return False
    args = ["worktree", "remove", path]
    if force:
        args.append("--force")
    result = _run(args, root, check=False)
    if result.returncode != 0:
        return False
    return True


def _review_range(board, card_id):
    """``base...card/<id>`` for review, or None when there is nothing to compare."""
    root = board.root
    if not is_repo(root):
        return None
    branch = board.branch_name(card_id)
    if not branch_exists(root, branch):
        return None
    base = board.read_config().get("base_branch") or "HEAD"
    if not branch_exists(root, base):
        return None
    return "%s...%s" % (base, branch)


def diffstat(board, card_id):
    """Summarise a card branch against the base branch, for review.

    Returns "" when there is nothing to compare, so the dashboard can simply
    print whatever comes back.
    """
    span = _review_range(board, card_id)
    if not span:
        return ""
    result = _run(["diff", "--stat", span], board.root, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def branch_paths(board, card_id):
    """Every path the card branch changes against the base branch."""
    span = _review_range(board, card_id)
    if not span:
        return []
    result = _run(["diff", "--name-only", span], board.root, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_paths(cwd):
    """Every path a commit run in ``cwd`` could pick up. For the scope guard.

    Staged files alone were not enough: the hook fires *before* the command,
    so ``git add -A && git commit`` and ``git commit -a`` had nothing staged
    yet and sailed through. Porcelain status covers staged, unstaged and
    untracked (not ignored) paths, relative to the repository root.
    """
    result = _run(
        ["status", "--porcelain", "-z", "--untracked-files=all"], cwd, check=False
    )
    if result.returncode != 0:
        return []
    paths = []
    entries = result.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and index < len(entries):
            paths.append(entries[index])  # a rename also touches its source
            index += 1
    return paths


def is_dirty(root):
    """True when the working tree has uncommitted changes."""
    result = _run(["status", "--porcelain"], root, check=False)
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())
