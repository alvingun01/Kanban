"""``kanban init``: set a board up inside a project folder.

Everything written here is either data (the board's own directories) or a
**stub** that defers to the installed package. Nothing scaffolded contains
logic, because a copy in someone's project is a copy that never gets upgraded.
``Dashboard.cmd`` is four lines; the skills defer to ``kanban brief``.

Files that belong to the project rather than to us -- ``CLAUDE.md``,
``.gitignore`` -- are appended to between markers and never clobbered. Re-init
rewrites only the marked span.
"""

import json
import os

from . import store
from .store import atomic_write, read_text

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MARK_START = ">>> kanban"
MARK_END = "<<< kanban"

#: Where the list of known boards lives. Resolved per call rather than at
#: import, and overridable via ``KANBAN_HOME``, so the test suite cannot write
#: into the real user's registry -- which it did, until this was fixed.
ENV_HOME = "KANBAN_HOME"


def registry_dir():
    override = os.environ.get(ENV_HOME)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".kanban")


def registry_path():
    return os.path.join(registry_dir(), "boards.json")

GITIGNORE_BLOCK = """\
# Runtime state: locks are machine-local, worktrees and transcripts are big,
# the dashboard render is generated, and backups are local safety copies.
cards/*.lock
wt/
runs/
board/
.kanban/backups/
"""

CLAUDE_MD_BLOCK = """\
## Kanban board

This project has an agent kanban board in `cards/`. One file per card, status
as a field, claimed via an `O_EXCL` lock.

**If you are an agent working a card, run `kanban brief agent` and follow it.**
That prints the current instructions from the installed tool, so they are never
stale.

Two rules matter enough to repeat here:

- `inbox -> ready` and anything leaving `needs_review` are **human-only**. They
  happen in the dashboard. Do not attempt them, and never set `KANBAN_HUMAN`.
- If a card needs to touch a path in its `spec.scope_deny`, stop and run
  `kanban ask <id> "<question>"`. Do not proceed anyway.
"""

DASHBOARD_CMD = """\
@echo off
REM Stub: all logic lives in the installed kanban package, so this file never
REM needs to change. Regenerate it with `kanban upgrade`.
setlocal
REM `cd` into this file's folder rather than passing --root "%~dp0".
REM %~dp0 always ends in a backslash, so "%~dp0" ends in \\" -- which the C
REM runtime reads as an escaped quote and folds into the string, producing a
REM path with a literal quote in it. The board then "does not exist" and the
REM window shuts before you can read why. `cd` tolerates the trailing slash,
REM and the board is found by searching upward from here.
cd /d "%~dp0"
set "PYTHONPATH={tool_root}"
"{python}" -m kanban serve --open
if errorlevel 1 (
  echo.
  echo The dashboard could not start. The reason is printed above.
  echo.
  pause
)
"""

DASHBOARD_SH = """\
#!/bin/sh
# Stub: all logic lives in the installed kanban package.
cd "$(dirname "$0")" || exit 1
PYTHONPATH="{tool_root}" exec "{python}" -m kanban serve --open
"""

def marked_block(body, comment="#"):
    """Wrap ``body`` in start/end markers using the given comment syntax."""
    if comment == "<!--":
        return "<!-- %s -->\n%s<!-- %s -->\n" % (MARK_START, body, MARK_END)
    return "%s %s\n%s%s %s\n" % (comment, MARK_START, body, comment, MARK_END)


def upsert_marked(path, body, comment="#"):
    """Append or replace our marked span in ``path``, leaving the rest alone."""
    block = marked_block(body, comment)
    start_token = MARK_START
    end_token = MARK_END

    if not os.path.isfile(path):
        atomic_write(path, block)
        return "created"

    existing = read_text(path)
    lines = existing.splitlines(keepends=True)
    start = end = None
    for index, line in enumerate(lines):
        if start is None and start_token in line:
            start = index
        elif start is not None and end_token in line:
            end = index
            break

    if start is not None and end is not None:
        replaced = "".join(lines[:start]) + block + "".join(lines[end + 1:])
        if replaced == existing:
            return "unchanged"
        atomic_write(path, replaced)
        return "updated"

    separator = "" if existing.endswith("\n") else "\n"
    atomic_write(path, existing + separator + "\n" + block)
    return "appended"


def register_board(root):
    """Record this board in ``~/.kanban/boards.json`` for ``upgrade --all``."""
    os.makedirs(registry_dir(), exist_ok=True)
    boards = []
    path = registry_path()
    if os.path.isfile(path):
        try:
            boards = json.loads(read_text(path)).get("boards", [])
        except (ValueError, AttributeError):
            boards = []
    root = os.path.abspath(root)
    if root not in boards:
        boards.append(root)
    atomic_write(
        path, json.dumps({"boards": sorted(boards)}, indent=2) + "\n"
    )
    return boards


def registered_boards():
    """Boards that still exist on disk, pruning ones that have gone."""
    path = registry_path()
    if not os.path.isfile(path):
        return []
    try:
        boards = json.loads(read_text(path)).get("boards", [])
    except (ValueError, AttributeError):
        return []
    return [
        path
        for path in boards
        if os.path.isfile(os.path.join(path, store.BOARD_DIR, store.CONFIG_NAME))
    ]


def _python_exe():
    import sys

    return sys.executable or "python"


def _current_branch(root):
    """The repo's current branch, or None if this is not a git repo."""
    from . import worktree

    if not worktree.is_repo(root):
        return None
    result = worktree._run(
        ["rev-parse", "--abbrev-ref", "HEAD"], root, check=False
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch and branch != "HEAD" else None


BIN_CMD = """\
@echo off
REM Board-local `kanban` shim. Dispatch puts this directory on a spawned
REM agent's PATH so it can just type `kanban`, with no shell quoting to get
REM wrong. Regenerated by `kanban upgrade`.
setlocal
set "PYTHONPATH={tool_root}"
"{python}" -m kanban %*
"""

BIN_SH = """\
#!/bin/sh
# Board-local `kanban` shim; see kanban.cmd.
PYTHONPATH="{tool_root}" exec "{python}" -m kanban "$@"
"""


def write_bin(board):
    """Write a board-local `kanban` shim for spawned agents to use.

    Without this, a spawned agent is handed a quoted
    ``"C:\\...\\python.exe" -m kanban`` string and has to get the quoting right
    in whichever shell it picks. In testing an agent burned four turns on that
    before giving up and reading the card file directly.
    """
    bin_dir = os.path.join(board.board_dir, "bin")
    values = {"tool_root": TOOL_ROOT, "python": _python_exe()}
    atomic_write(os.path.join(bin_dir, "kanban.cmd"), BIN_CMD.format(**values))
    sh_path = os.path.join(bin_dir, "kanban")
    atomic_write(sh_path, BIN_SH.format(**values))
    try:
        os.chmod(sh_path, 0o755)
    except OSError:
        pass
    return {".kanban/bin/kanban": "written"}


def write_launchers(root):
    """Write the double-clickable dashboard stubs."""
    actions = {}
    cmd_path = os.path.join(root, "Dashboard.cmd")
    atomic_write(
        cmd_path,
        DASHBOARD_CMD.format(tool_root=TOOL_ROOT, python=_python_exe()),
    )
    actions["Dashboard.cmd"] = "written"

    sh_path = os.path.join(root, "dashboard.sh")
    atomic_write(
        sh_path, DASHBOARD_SH.format(tool_root=TOOL_ROOT, python=_python_exe())
    )
    try:
        os.chmod(sh_path, 0o755)
    except OSError:
        pass
    actions["dashboard.sh"] = "written"
    return actions


def write_vscode_task(root):
    """Add a "Kanban Dashboard" task so it can be launched inside VSCode."""
    path = os.path.join(root, ".vscode", "tasks.json")
    command = '"%s" -m kanban serve --open --root "${workspaceFolder}"' % (
        _python_exe(),
    )
    task = {
        "label": "Kanban Dashboard",
        "type": "shell",
        "command": command,
        "options": {"env": {"PYTHONPATH": TOOL_ROOT}},
        "problemMatcher": [],
        "presentation": {"reveal": "silent", "panel": "dedicated"},
    }

    data = {"version": "2.0.0", "tasks": []}
    if os.path.isfile(path):
        try:
            data = json.loads(read_text(path))
        except ValueError:
            # A tasks.json with comments in it is valid for VSCode but not for
            # json.loads. Leave the user's file alone rather than mangling it.
            return "skipped (not plain JSON)"
    data.setdefault("version", "2.0.0")
    tasks = data.setdefault("tasks", [])
    tasks = [item for item in tasks if item.get("label") != "Kanban Dashboard"]
    tasks.append(task)
    data["tasks"] = tasks
    atomic_write(path, json.dumps(data, indent=2) + "\n")
    return "written"


HOOK_STUB = '''\
"""Stub. The scope-guard logic lives in the installed kanban package, so this
file never needs to change -- `kanban upgrade` regenerates it if the tool moves.

Blocks a `git commit` whose staged files match the claimed card's scope_deny.
"""
import sys

sys.path.insert(0, r"{tool_root}")

from kanban.hooks import run

sys.exit(run("scope-guard"))
'''

SKILL_KANBAN = """\
---
name: kanban
description: >-
  Work a card on this project's agent kanban board: claim it, do the work in its
  git worktree, record what you learned, and submit it for human review. Use
  when asked to pull, claim, or work a kanban card, or when given a card id.
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

Run this first and follow it exactly:

```
{kanban} brief agent
```

It prints the current operating instructions from the installed tool, so they
are never out of date. Everything you need -- the claim/heartbeat/submit loop,
the scope rules, and which transitions are human-only -- is in there.
"""

SKILL_PLANNER = """\
---
name: planner
description: >-
  Break a goal into kanban cards for agents to execute. Use when given a feature
  or objective that needs decomposing into separate pieces of work, rather than a
  single card to execute.
allowed-tools: Bash, Read, Grep, Glob
---

Run this first and follow it exactly:

```
{kanban} brief planner
```

It prints the current instructions from the installed tool. In short: read
enough to decompose honestly, show the human your proposed breakdown and wait
for agreement, then create the cards. They land in the inbox -- only the human
promotes them.
"""


def _kanban_command():
    """How a skill or hook should invoke this tool from a project."""
    import shutil

    if shutil.which("kanban"):
        return "kanban"
    return 'PYTHONPATH="%s" "%s" -m kanban' % (TOOL_ROOT, _python_exe())


def write_skills(root):
    """Write the thin skill files. Bodies defer to `kanban brief`."""
    command = _kanban_command()
    report = {}
    for name, template in (
        ("kanban", SKILL_KANBAN),
        ("planner", SKILL_PLANNER),
    ):
        path = os.path.join(root, ".claude", "skills", name, "SKILL.md")
        atomic_write(path, template.format(kanban=command))
        report[".claude/skills/%s/SKILL.md" % name] = "written"
    return report


def write_hook(root):
    """Write the scope-guard stub and register it in .claude/settings.json.

    Uses the hook's **exec form** (`command` plus `args`) rather than a shell
    string: on Windows the shell form goes through Git Bash or PowerShell, and
    this project's own path contains spaces, so avoiding shell quoting removes
    a whole class of breakage.
    """
    stub_path = os.path.join(root, ".claude", "hooks", "scope-guard.py")
    atomic_write(stub_path, HOOK_STUB.format(tool_root=TOOL_ROOT))

    settings_path = os.path.join(root, ".claude", "settings.json")
    settings = {}
    if os.path.isfile(settings_path):
        try:
            settings = json.loads(read_text(settings_path))
        except ValueError:
            # Never mangle a settings file we cannot parse.
            return {
                ".claude/hooks/scope-guard.py": "written",
                ".claude/settings.json": "SKIPPED (not plain JSON) -- add the "
                "hook by hand",
            }

    entry = {
        "type": "command",
        # Permission-rule syntax; narrows the Bash matcher to commits only.
        "if": "Bash(git commit *)",
        "command": _python_exe(),
        "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/scope-guard.py"],
    }

    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        pre = []
        hooks["PreToolUse"] = pre

    # Replace only our own entry, leaving anyone else's hooks alone.
    for group in pre:
        if not isinstance(group, dict):
            continue
        inner = group.get("hooks")
        if not isinstance(inner, list):
            continue
        kept = [
            item
            for item in inner
            if not (
                isinstance(item, dict)
                and "scope-guard.py" in json.dumps(item.get("args") or item.get("command") or "")
            )
        ]
        group["hooks"] = kept
    pre[:] = [
        group
        for group in pre
        if not (isinstance(group, dict) and group.get("hooks") == [])
    ]
    pre.append({"matcher": "Bash", "hooks": [entry]})

    atomic_write(settings_path, json.dumps(settings, indent=2) + "\n")
    return {
        ".claude/hooks/scope-guard.py": "written",
        ".claude/settings.json": "hook registered",
    }


def init(root, force_config=False, **config_overrides):
    """Initialise or refresh a board. Idempotent.

    Returns a list of (label, outcome) describing what happened, so ``init``
    and ``upgrade`` can both report honestly.
    """
    root = os.path.abspath(root)
    report = []

    existed = os.path.isfile(
        os.path.join(root, store.BOARD_DIR, store.CONFIG_NAME)
    )
    # Default base_branch to whatever this repo actually uses, rather than
    # guessing "main" and silently falling back to HEAD on a master repo.
    if "base_branch" not in config_overrides:
        detected = _current_branch(root)
        if detected:
            config_overrides["base_branch"] = detected
    board = store.Board.create(root, **config_overrides)
    report.append((".kanban/config.json", "kept" if existed else "created"))

    for directory in (board.cards_dir, board.runs_dir):
        os.makedirs(directory, exist_ok=True)
    report.append(("cards/, runs/", "ready"))

    report.append(
        (".gitignore", upsert_marked(
            os.path.join(root, ".gitignore"), GITIGNORE_BLOCK, "#"
        ))
    )
    report.append(
        ("CLAUDE.md", upsert_marked(
            os.path.join(root, "CLAUDE.md"), CLAUDE_MD_BLOCK, "<!--"
        ))
    )

    for label, outcome in write_bin(board).items():
        report.append((label, outcome))
    for label, outcome in write_launchers(root).items():
        report.append((label, outcome))
    report.append((".vscode/tasks.json", write_vscode_task(root)))
    for label, outcome in write_skills(root).items():
        report.append((label, outcome))
    for label, outcome in write_hook(root).items():
        report.append((label, outcome))

    register_board(root)
    report.append(("~/.kanban/boards.json", "registered"))
    return board, report
