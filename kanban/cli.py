"""Command line interface.

This is the **agent-facing** surface. Humans use the dashboard; the human-only
transitions (promote, approve, reject, answer) are exposed here only so tests
and scripts can drive them via ``KANBAN_HUMAN=1``, and they refuse otherwise.

Every command prints for people by default and accepts ``--json`` where an
agent would want to parse the result.
"""

import argparse
import json
import os
import sys

from . import lease, locks, model, scaffold, scope, store, transitions, worktree
from .actor import Actor
from .model import parse_ts

COLUMN_LABELS = {
    "inbox": "Inbox",
    "ready": "Execute",
    "claimed": "Running",
    "needs_review": "Review",
    "merged": "Done",
}


def _force_utf8():
    """The audit log contains arrows; a cp1252 console would crash on them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _board(args, check=True):
    board = store.Board.find(getattr(args, "root", None))
    if check:
        board.check_compat()
    return board


def _actor(args):
    return Actor.from_env(getattr(args, "id", None))


def _print(text=""):
    print(text)


def _emit_json(payload):
    print(json.dumps(payload, indent=2, default=str))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(args):
    root = os.path.abspath(args.path or os.getcwd())
    board, report = scaffold.init(root)
    _print("board ready in %s" % root)
    for label, outcome in report:
        _print("  %-24s %s" % (label, outcome))
    if not worktree.is_repo(root):
        _print()
        _print(
            "note: not a git repository, so cards will run without worktrees."
        )
        _print("      run `git init` first if you want per-card branches.")
    _print()
    _print("next: double-click Dashboard.cmd (everything happens there)")
    return 0


def cmd_new(args):
    board = _board(args)
    card_id = args.id or board.next_id()
    card = model.new_card(
        card_id,
        args.title,
        goal=args.goal or "",
        acceptance=args.acceptance or [],
        origin=args.origin,
        tier="act" if args.skip_planning else "plan",
        read_only=args.read_only,
        parent=args.parent,
        depends_on=args.depends_on or [],
        scope_allow=args.scope_allow or [],
        scope_deny=args.scope_deny or [],
        skills=args.skills or [],
        max_turns=args.max_turns,
    )
    model.require_valid(card)
    board.create_card_file(card, "")
    if args.json:
        _emit_json({"id": card_id, "status": card["status"], "tier": card["tier"]})
    else:
        _print("created card %s in inbox (tier: %s)" % (card_id, card["tier"]))
        _print("promote it from the dashboard to start an agent on it")
    return 0


def cmd_list(args):
    board = _board(args)
    config = board.read_config()
    rows = []
    broken = []
    for card_id, card, body in board.all_cards():
        if card is None:
            broken.append((card_id, body))
            continue
        if args.status and card.get("status") != args.status:
            continue
        if args.claimable:
            if card.get("status") != "ready":
                continue
            if transitions.blocking_deps(board, card):
                continue
        rows.append((card_id, card))

    if args.json:
        _emit_json(
            {
                "cards": [
                    {
                        "id": card_id,
                        "status": card.get("status"),
                        "tier": card.get("tier"),
                        "title": card.get("title"),
                        "owner": (card.get("lease") or {}).get("owner"),
                        "attempts": card.get("attempts"),
                        "skills": (card.get("spec") or {}).get("skills") or [],
                        "blocked_by": [
                            dep for dep, _ in transitions.blocking_deps(board, card)
                        ],
                    }
                    for card_id, card in rows
                ],
                "unreadable": [{"id": i, "error": e} for i, e in broken],
            }
        )
        return 0

    if not rows and not broken:
        _print("no cards yet -- create one in the dashboard")
        return 0

    order = list(COLUMN_LABELS)
    rows.sort(key=lambda item: (order.index(item[1].get("status", "inbox")), item[0]))
    for card_id, card in rows:
        status = card.get("status", "?")
        blockers = transitions.blocking_deps(board, card)
        flags = []
        if card.get("tier") == "plan":
            flags.append("plan-first")
        if blockers:
            flags.append("blocked by %s" % transitions.describe_blockers(blockers))
        owner = (card.get("lease") or {}).get("owner")
        if owner:
            flags.append(owner)
        review = card.get("review") or {}
        if review.get("reason"):
            flags.append("review: %s" % review["reason"])
        suffix = ("  [%s]" % "; ".join(flags)) if flags else ""
        _print(
            "%-5s %-9s %-40s%s"
            % (card_id, COLUMN_LABELS.get(status, status), card.get("title", ""), suffix)
        )

    for card_id, error in broken:
        _print("%-5s %-9s UNREADABLE: %s" % (card_id, "!", error))
    return 0


def cmd_show(args):
    board = _board(args)
    card, body = board.load(args.id)
    if args.json:
        _emit_json({"card": card, "body": body})
        return 0
    _print(model.dumps(card, body))
    return 0


def cmd_claim(args):
    board = _board(args)
    actor = _actor(args)
    card, body = lease.claim(board, args.id, actor, force=args.force)
    artifacts = card.get("artifacts") or {}
    if args.json:
        _emit_json(
            {
                "id": args.id,
                "status": card["status"],
                "tier": card.get("tier"),
                "worktree": artifacts.get("worktree"),
                "branch": artifacts.get("branch"),
                "lease": card.get("lease"),
                "goal": (card.get("spec") or {}).get("goal"),
                "acceptance": (card.get("spec") or {}).get("acceptance"),
                "scope_deny": (card.get("spec") or {}).get("scope_deny"),
                "skills": (card.get("spec") or {}).get("skills"),
            }
        )
        return 0
    _print("claimed %s (tier: %s)" % (args.id, card.get("tier")))
    if artifacts.get("worktree"):
        _print("  worktree: %s  branch: %s" % (
            artifacts["worktree"], artifacts.get("branch")
        ))
    _print("  lease expires %s -- run `kanban heartbeat %s` while working"
           % ((card.get("lease") or {}).get("expires_at"), args.id))
    return 0


def cmd_release(args):
    board = _board(args)
    lease.release(board, args.id, _actor(args), args.reason or "released")
    _print("released %s back to Execute" % args.id)
    return 0


def cmd_heartbeat(args):
    board = _board(args)
    card, _ = lease.heartbeat(board, args.id, _actor(args))
    _print("heartbeat %s -- lease now expires %s"
           % (args.id, (card.get("lease") or {}).get("expires_at")))
    return 0


def cmd_note(args):
    board = _board(args)
    card, body = board.load(args.id)
    body = model.append_note(body, _actor(args).name, args.text)
    board.save(args.id, card, body)
    _print("noted on %s" % args.id)
    return 0


def cmd_ask(args):
    board = _board(args)
    transitions.submit_question(board, args.id, _actor(args), args.question)
    _print("card %s is now in Review, waiting on a human answer" % args.id)
    return 0


def cmd_submit(args):
    board = _board(args)
    actor = _actor(args)
    card, _ = board.load(args.id)
    tier = card.get("tier")

    plan_text = args.plan
    if args.plan_file:
        plan_text = store.read_text(args.plan_file)

    as_plan = args.as_plan or (tier == "plan" and not args.as_work)
    if as_plan:
        if not (plan_text or "").strip():
            _print(
                "error: this card is tier 'plan', so submit a plan:\n"
                "       kanban submit %s --plan \"...\"  (or --plan-file FILE)\n"
                "       use --work only once the plan has been approved."
                % args.id,
                )
            return 1
        transitions.submit_plan(board, args.id, actor, plan_text)
        _print("plan submitted for %s -- waiting for approval in the dashboard"
               % args.id)
        return 0

    transitions.submit_work(board, args.id, actor, args.detail or "")
    _print("work submitted for %s -- waiting for review in the dashboard"
           % args.id)
    return 0


def cmd_sweep(args):
    board = _board(args)
    reaped = lease.sweep(board)
    if not reaped:
        _print("no stale leases")
        return 0
    for card_id, outcome in reaped:
        _print("%s: %s" % (card_id, outcome))
    return 0


def cmd_scope_check(args):
    board = _board(args)
    card, _ = board.load(args.id)
    allow, deny = scope.card_scope(card)
    report = scope.check_paths(args.paths, allow, deny)

    if args.json:
        _emit_json(
            {
                "denied": [
                    {"path": p, "pattern": pat} for p, pat in report[scope.DENIED]
                ],
                "outside": [p for p, _ in report[scope.OUTSIDE]],
                "allowed": [p for p, _ in report[scope.ALLOWED]],
            }
        )
        return 1 if report[scope.DENIED] else 0

    for path, pattern in report[scope.DENIED]:
        _print("DENIED  %s  (matches scope_deny %s)" % (path, pattern))
    for path, _ in report[scope.OUTSIDE]:
        _print("OUTSIDE %s  (not in scope_allow)" % path)
    for path, _ in report[scope.ALLOWED]:
        _print("ok      %s" % path)

    if report[scope.DENIED]:
        _print()
        _print("This card must not touch those paths. Do not work around it --")
        _print('run: kanban ask %s "<why you need that path>"' % args.id)
        return 1
    return 0


def cmd_check(args):
    board = _board(args)
    problems = []
    skills = _available_skills(board.root)

    for card_id, card, body in board.all_cards():
        if card is None:
            problems.append("%s: unreadable (%s)" % (card_id, body))
            continue
        for issue in model.validate(card):
            problems.append("%s: %s" % (card_id, issue))
        if str(card.get("id")) != str(card_id):
            problems.append(
                "%s: frontmatter id is %r but the filename says %s"
                % (card_id, card.get("id"), card_id)
            )
        for name in (card.get("spec") or {}).get("skills") or []:
            if skills and name not in skills:
                problems.append(
                    "%s: spec.skills names %r, which has no SKILL.md "
                    "(agents would silently work without it)" % (card_id, name)
                )
        for dep, status in transitions.blocking_deps(board, card):
            if status == "missing":
                problems.append(
                    "%s: depends_on %s does not exist" % (card_id, dep)
                )

    problems.extend(lease.audit_locks(board))

    if args.json:
        _emit_json({"problems": problems, "ok": not problems})
        return 1 if problems else 0

    if not problems:
        _print("board is consistent (%d cards)" % len(board.card_ids()))
        return 0
    for problem in problems:
        _print("problem: %s" % problem)
    return 1


def _available_skills(root):
    """Skill names discoverable from this project and the user's home."""
    found = set()
    for base in (
        os.path.join(root, ".claude", "skills"),
        os.path.join(os.path.expanduser("~"), ".claude", "skills"),
    ):
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if os.path.isfile(os.path.join(base, name, "SKILL.md")):
                found.add(name)
    return found


# -- human-only, exposed for tests and scripting only ----------------------

def _require_human(actor):
    if not actor.is_human:
        _print(
            "error: this is a human-only transition -- do it in the dashboard.\n"
            "       (Scripts and tests can set KANBAN_HUMAN=1. Never set that\n"
            "        anywhere an agent can read it: it is the review gate.)",
            )
        return False
    return True


def cmd_promote(args):
    board = _board(args)
    actor = _actor(args)
    if not _require_human(actor):
        return 1
    ids = args.ids
    if args.parent:
        ids = [
            card_id
            for card_id, card, _ in board.all_cards()
            if card and card.get("parent") == args.parent
            and card.get("status") == "inbox"
        ]
        if not ids:
            _print("no inbox cards with parent %s" % args.parent)
            return 0
    for card_id in ids:
        transitions.promote(board, card_id, actor)
        _print("promoted %s to Execute" % card_id)
    return 0


def cmd_approve(args):
    board = _board(args)
    actor = _actor(args)
    if not _require_human(actor):
        return 1
    card, _ = transitions.approve(board, args.id, actor)
    _print("approved %s -> %s" % (args.id, card["status"]))
    return 0


def cmd_reject(args):
    board = _board(args)
    actor = _actor(args)
    if not _require_human(actor):
        return 1
    transitions.reject(board, args.id, actor, args.feedback)
    _print("rejected %s back to Execute, feedback recorded" % args.id)
    return 0


def cmd_answer(args):
    board = _board(args)
    actor = _actor(args)
    if not _require_human(actor):
        return 1
    transitions.answer(board, args.id, actor, args.text)
    _print("answered %s, back to Execute" % args.id)
    return 0


def cmd_upgrade(args):
    from . import migrations

    roots = (
        scaffold.registered_boards()
        if args.all
        else [store.Board.find(getattr(args, "root", None)).root]
    )
    if not roots:
        _print("no boards registered -- run `kanban init` in a project first")
        return 0

    failures = 0
    for root in roots:
        board = store.Board(root)
        _print("== %s" % root)
        try:
            report = migrations.migrate_board(
                board, dry_run=args.dry_run, allow_dirty=args.allow_dirty
            )
        except migrations.MigrationError as exc:
            _print("  error: %s" % exc)
            failures += 1
            continue

        if report.get("already_current"):
            _print("  cards already at schema v%d" % report["to"])
        else:
            verb = "would migrate" if args.dry_run else "migrated"
            _print(
                "  %s %d card(s) from v%d to v%d"
                % (verb, len(report["migrated"]), report["from"], report["to"])
            )
            for item in report["migrated"]:
                if item["changes"]:
                    _print("    %s" % item["id"])
                    for change in item["changes"]:
                        _print("      %s" % change)
            if report.get("snapshot"):
                _print("  snapshot: %s" % report["snapshot"])

        if not args.dry_run:
            _, stub_report = scaffold.init(root)
            changed = [
                "%s (%s)" % (label, outcome)
                for label, outcome in stub_report
                if outcome in ("updated", "created", "written")
            ]
            if changed:
                _print("  refreshed: %s" % ", ".join(changed))

    return 1 if failures else 0


def cmd_update(args):
    from . import updater

    try:
        result = updater.update(args.source)
    except updater.UpdateError as exc:
        _print("error: %s" % exc)
        return 1

    _print("tool: %s" % result["status"])
    _print("version: %s" % result["version"])
    if not result["needs_upgrade"]:
        _print("all registered boards are at the current schema")
        return 0

    _print("boards needing migration:")
    for root, version in result["needs_upgrade"]:
        _print("  v%d  %s" % (version, root))
    if args.no_upgrade:
        _print("run `kanban upgrade --all` when ready")
        return 0

    _print("")
    args.all = True
    args.dry_run = False
    args.allow_dirty = False
    return cmd_upgrade(args)


def cmd_rollback(args):
    from . import migrations

    board = _board(args, check=False)
    target = migrations.rollback(board, args.snapshot)
    _print("restored cards/ from %s" % target)
    _print("note: .kanban/config.json schema_version was not changed")
    return 0


def cmd_doctor(args):
    from . import dispatch

    board = _board(args, check=False)
    config = board.read_config()
    _print("board      %s" % board.root)
    _print("schema     board v%s, tool v%s"
           % (config.get("schema_version"), model.SCHEMA_VERSION))

    binary = dispatch.resolve_claude_bin(config)
    _print("agents     %s" % (binary or "NOT FOUND -- agents cannot start"))
    if binary and "vscode/extensions" in binary.replace("\\", "/"):
        _print("           (bundled with the VSCode extension; this path moves")
        _print("            when the extension updates, and is re-resolved each")
        _print("            run rather than stored)")
    kanban_cmd, env = dispatch.kanban_invocation(board)
    _print("agents use `%s`%s" % (
        kanban_cmd,
        "  (via .kanban/bin on their PATH)" if env.get("PATH") else "",
    ))
    _print("perm mode  %s" % config.get("permission_mode"))
    allowed = list(dispatch.ALLOWED_TOOLS) + list(
        config.get("extra_allowed_tools") or []
    )
    _print("agents may %s" % ", ".join(allowed))
    _print("limits     %s concurrent, %s attempts, lease %ss"
           % (config.get("max_concurrent_agents"), config.get("max_attempts"),
              config.get("lease_ttl_seconds")))
    _print("git        %s"
           % ("repo, base branch %s" % config.get("base_branch")
              if worktree.is_repo(board.root) else "not a git repo (no worktrees)"))
    if os.environ.get("KANBAN_HUMAN"):
        _print()
        _print("WARNING    KANBAN_HUMAN is set in this environment.")
        _print("           That bypasses the review gate. Unset it unless you")
        _print("           are deliberately running tests.")
    return 0


def cmd_serve(args):
    from . import server

    board = _board(args)
    return server.serve(
        board,
        port=args.port,
        open_browser=args.open,
        auto_dispatch=not args.no_dispatch,
    )


def cmd_hook(args):
    from . import hooks

    return hooks.run(args.name)


def cmd_brief(args):
    from . import briefs

    text = briefs.get(args.audience)
    if text is None:
        _print("error: no brief called %r" % args.audience)
        return 1
    _print(text)
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="kanban", description="Agent kanban board."
    )
    parser.add_argument(
        "--root", help="board directory (default: search upward from cwd)"
    )

    # --root must work on either side of the subcommand: the generated
    # Dashboard.cmd passes it after. SUPPRESS keeps the subparser's absent
    # default from clobbering a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subs = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        sub = subs.add_parser(name, help=help_text, parents=[common])
        sub.set_defaults(func=func)
        return sub

    sub = add("init", cmd_init, "set up a board in this folder")
    sub.add_argument("path", nargs="?", help="folder to initialise")

    sub = add("new", cmd_new, "create a card in the inbox")
    sub.add_argument("title")
    sub.add_argument("--id", help="explicit card id (default: next free)")
    sub.add_argument("--goal", help="what an agent with no context must do")
    sub.add_argument("--acceptance", action="append", metavar="COND")
    sub.add_argument("--scope-allow", action="append", metavar="GLOB")
    sub.add_argument("--scope-deny", action="append", metavar="GLOB")
    sub.add_argument("--skills", action="append", metavar="NAME")
    sub.add_argument("--depends-on", action="append", metavar="ID")
    sub.add_argument("--parent", help="goal id, for planner-decomposed cards")
    sub.add_argument("--origin", choices=model.ORIGINS, default="human")
    sub.add_argument("--max-turns", type=int, default=40)
    sub.add_argument(
        "--skip-planning",
        action="store_true",
        help="card is already specified; start at tier 'act'",
    )
    sub.add_argument(
        "--read-only",
        action="store_true",
        help="the agent may look but not change anything",
    )
    sub.add_argument("--json", action="store_true")

    sub = add("list", cmd_list, "list cards")
    sub.add_argument("--status", choices=model.STATUSES)
    sub.add_argument(
        "--claimable",
        action="store_true",
        help="only ready cards whose dependencies are merged",
    )
    sub.add_argument("--json", action="store_true")

    sub = add("show", cmd_show, "print one card")
    sub.add_argument("id")
    sub.add_argument("--json", action="store_true")

    sub = add("claim", cmd_claim, "claim a ready card")
    sub.add_argument("id")
    sub.add_argument(
        "--force", action="store_true", help="claim despite unmerged deps"
    )
    sub.add_argument("--json", action="store_true")

    sub = add("release", cmd_release, "give a claimed card back")
    sub.add_argument("id")
    sub.add_argument("--reason")

    sub = add("heartbeat", cmd_heartbeat, "refresh a lease while working")
    sub.add_argument("id")

    sub = add("note", cmd_note, "append a finding to the card's Context")
    sub.add_argument("id")
    sub.add_argument("text")

    sub = add("ask", cmd_ask, "stop and ask the human a question")
    sub.add_argument("id")
    sub.add_argument("question")

    sub = add("submit", cmd_submit, "submit a plan or finished work")
    sub.add_argument("id")
    sub.add_argument("--plan", help="plan text (tier 'plan' cards)")
    sub.add_argument("--plan-file", help="read plan text from a file")
    sub.add_argument("--as-plan", action="store_true")
    sub.add_argument("--as-work", action="store_true")
    sub.add_argument("--detail", help="short note for the audit log")

    add("sweep", cmd_sweep, "reap expired leases")

    sub = add("scope-check", cmd_scope_check, "test paths against a card's scope")
    sub.add_argument("id")
    sub.add_argument("paths", nargs="+")
    sub.add_argument("--json", action="store_true")

    sub = add("check", cmd_check, "validate every card on the board")
    sub.add_argument("--json", action="store_true")

    sub = add("serve", cmd_serve, "run the dashboard")
    sub.add_argument("--port", type=int)
    sub.add_argument("--open", action="store_true", help="open a browser")
    sub.add_argument(
        "--no-dispatch",
        action="store_true",
        help="never start agents (browse and review only)",
    )

    sub = add("brief", cmd_brief, "print agent instructions")
    sub.add_argument("audience", choices=("agent", "planner"))

    sub = add("upgrade", cmd_upgrade, "migrate cards and refresh stubs")
    sub.add_argument(
        "--all", action="store_true", help="every board in the registry"
    )
    sub.add_argument(
        "--dry-run", action="store_true", help="show the changes, write nothing"
    )
    sub.add_argument(
        "--allow-dirty",
        action="store_true",
        help="migrate even with uncommitted changes (not recommended)",
    )

    sub = add("update", cmd_update, "install a tool patch, then migrate boards")
    sub.add_argument(
        "--from",
        dest="source",
        metavar="ZIP|DIR",
        help="patch to install (offline; no git remote needed)",
    )
    sub.add_argument(
        "--no-upgrade",
        action="store_true",
        help="install the patch but do not migrate boards yet",
    )

    sub = add("rollback", cmd_rollback, "restore cards/ from a snapshot")
    sub.add_argument("snapshot", nargs="?", help="path (default: newest)")

    add("doctor", cmd_doctor, "check this board's setup")

    sub = add("hook", cmd_hook, "internal: run a Claude Code hook")
    sub.add_argument("name", choices=("scope-guard",))

    sub = add("promote", cmd_promote, "inbox -> Execute (human only)")
    sub.add_argument("ids", nargs="*")
    sub.add_argument("--parent", help="promote every inbox card with this parent")

    sub = add("approve", cmd_approve, "approve a plan or finished work (human)")
    sub.add_argument("id")

    sub = add("reject", cmd_reject, "send work back with feedback (human only)")
    sub.add_argument("id")
    sub.add_argument("feedback")

    sub = add("answer", cmd_answer, "answer a card's question (human only)")
    sub.add_argument("id")
    sub.add_argument("text")

    return parser


HANDLED = (
    store.BoardError,
    model.CardError,
    transitions.TransitionError,
    lease.ClaimRefused,
    locks.ClaimConflict,
    worktree.GitError,
    OSError,
)


def main(argv=None):
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except HANDLED as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
