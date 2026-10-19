"""The dashboard's view model.

The browser renders whatever this produces and nothing more. In particular the
**available actions are decided here, server-side**, so the UI can only offer
legal transitions -- clicking around cannot drive a card into an invalid state,
and a stale page cannot invent a button.

Turn and cost figures come from the agent's own transcript. On a subscription
the dollar figure is list-price accounting rather than a bill, so it is shown
as an observability number, not a budget.
"""

import datetime
import json
import os

from . import dispatch, lease, locks, model, pr, scope, transitions, worktree
from .model import parse_ts

COLUMNS = (
    ("inbox", "Inbox"),
    ("ready", "Execute"),
    ("claimed", "Running"),
    ("needs_review", "Review"),
    ("merged", "Done"),
)


def _age(timestamp, now):
    """Human-readable age, or None."""
    when = parse_ts(timestamp)
    if when is None:
        return None
    seconds = int((now - when).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd" % (seconds // 86400)


#: path -> ((size, mtime), stats). The dashboard polls every few seconds and
#: used to re-parse every transcript on the board each time, Done cards too.
_STATS_CACHE = {}


def read_run_stats(board, card_id):
    """Pull turns and list-price cost from the agent's transcript, if any."""
    path = board.run_path(card_id)
    try:
        info = os.stat(path)
    except OSError:
        return {}
    key = (info.st_size, info.st_mtime_ns)
    cached = _STATS_CACHE.get(path)
    if cached and cached[0] == key:
        return dict(cached[1])
    stats = _scan_transcript(path)
    _STATS_CACHE[path] = (key, stats)
    return dict(stats)


def _scan_transcript(path):
    turns = None
    cost = None
    result = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if "num_turns" in event:
                    turns = event["num_turns"]
                if "total_cost_usd" in event:
                    cost = event["total_cost_usd"]
                if event.get("type") == "result" and isinstance(event.get("result"), str):
                    result = event["result"]
    except OSError:
        return {}
    stats = {}
    if turns is not None:
        stats["turns"] = turns
    if cost is not None:
        stats["cost_usd"] = round(float(cost), 4)
    if result:
        stats["result"] = result
    return stats


def actions_for(card, blocked, dispatch_available, has_changes=True,
                starting=False, violations=(), pr_url=None, pr_checked=False):
    """Which buttons this card may show. The only source of truth for the UI."""
    status = card.get("status")
    tier = card.get("tier")
    review = card.get("review") or {}
    reason = review.get("reason")

    if status == "inbox":
        label = "Plan it" if tier == "plan" else "Execute"
        hint = (
            "An agent will investigate and come back with a plan for you to "
            "approve." if tier == "plan"
            else "An agent will start work on this now."
        )
        return [
            {
                "action": "execute",
                "label": label,
                "kind": "primary",
                "hint": hint,
                "disabled": bool(blocked),
                "disabled_reason": (
                    "blocked by %s" % transitions.describe_blockers(blocked)
                    if blocked else None
                ),
            }
        ]

    if status == "ready":
        return [
            {
                "action": "start",
                "label": "Start now",
                "kind": "secondary",
                "hint": "Queued. Start an agent immediately instead of waiting "
                        "for a free slot.",
                "disabled": bool(blocked) or starting or not dispatch_available,
                "disabled_reason": (
                    "blocked by %s" % transitions.describe_blockers(blocked)
                    if blocked else
                    "an agent is already starting" if starting else
                    None if dispatch_available else "agent runner unavailable"
                ),
            }
        ]

    if status == "claimed":
        return [
            {
                "action": "release",
                "label": "Release",
                "kind": "secondary",
                "hint": "Stops this agent and puts the card back in Execute, "
                        "where another agent picks it up.",
                "confirm": "Stop the agent and send this card back to Execute?",
                "disabled": False,
            },
            {
                "action": "to_inbox",
                "label": "Move to Inbox",
                "kind": "secondary",
                "hint": "Stops this agent and puts the card back in Inbox. "
                        "Nothing starts until you click Execute again. "
                        "Work so far is kept.",
                "confirm": "Stop the agent and move this card back to Inbox?",
                "disabled": False,
            },
        ]

    if status == "needs_review":
        if reason == "question":
            return [
                {
                    "action": "answer",
                    "label": "Send answer",
                    "kind": "primary",
                    "needs_text": True,
                    "placeholder": "Your answer...",
                    "hint": "The agent resumes with your answer on the card.",
                    "disabled": False,
                },
                {
                    "action": "reject",
                    "label": "Reject",
                    "kind": "danger",
                    "needs_text": True,
                    "placeholder": "What is wrong...",
                    "hint": "Send it back without answering.",
                    "disabled": False,
                },
            ]
        if reason == "plan":
            return [
                {
                    "action": "approve",
                    "label": "Approve plan",
                    "kind": "primary",
                    "hint": "Accept the plan; the card returns to Execute and "
                            "an agent carries it out.",
                    "disabled": False,
                },
                {
                    "action": "reject",
                    "label": "Reject",
                    "kind": "danger",
                    "needs_text": True,
                    "placeholder": "What should change...",
                    "hint": "Send the plan back with feedback.",
                    "disabled": False,
                },
            ]
        approve = {
            "action": "approve",
            "label": "Approve" if has_changes else "Accept answer",
            "kind": "primary",
            "hint": (
                "Accept the change and move the card to Done."
                if has_changes else
                "Nothing was changed. Accept the agent's answer and close the card."
            ),
            "disabled": False,
        }
        confirm = []
        if pr_url:
            approve["hint"] = (
                "Merge the pull request on GitHub, then approve to move the "
                "card to Done." + (" Approve checks that it was merged." if pr_checked else "")
            )
            if not pr_checked:
                # Without gh nobody can check, so ask the one person who knows.
                confirm.append("Approving marks the card Done. Have you merged "
                               "its pull request on GitHub?")
        if violations:
            confirm.insert(0, (
                "This branch changes %d path(s) the card must not touch: %s. "
                "Approve anyway?" % (
                    len(violations),
                    ", ".join(item["path"] for item in violations[:5]),
                )
            ))
        if confirm:
            approve["confirm"] = "\n\n".join(confirm)
        return [
            approve,
            {
                "action": "reject",
                "label": "Reject",
                "kind": "danger",
                "needs_text": True,
                "placeholder": "What is wrong...",
                "hint": "Sends the card back to an agent with your feedback. "
                        "Anything already done is kept.",
                "disabled": False,
            },
        ]

    return []


def asking(card, has_changes, pr_url=None):
    """What, if anything, this card needs from the human -- in one sentence."""
    if card.get("status") != "needs_review":
        return ""
    reason = (card.get("review") or {}).get("reason")
    if reason == "question":
        return "The agent is stuck and needs your answer before it can carry on."
    if reason == "plan":
        return ("The agent wants your OK on its plan before it starts. Approve to "
                "let it go ahead, or reject with what to change.")
    if has_changes and pr_url:
        return ("The agent finished and opened a pull request. Review and merge "
                "it on GitHub, then approve here, or reject with what is wrong.")
    if has_changes:
        return ("The agent finished and changed files. Check the changes, then "
                "approve them or reject with what is wrong.")
    return ("The agent finished and changed nothing - its answer is the result. "
            "Read it, then accept to close the card, or reject to send it back "
            "with a follow-up.")


def card_view(board, card_id, card, body, now, config, dispatch_available):
    """Build one card's payload."""
    preamble, sections, _ = model.split_sections(body)
    spec = card.get("spec") or {}
    artifacts = card.get("artifacts") or {}
    lease_info = card.get("lease") or {}
    review = card.get("review") or {}
    blocked = transitions.blocking_deps(board, card)

    lock = locks.read(board.lock_path(card_id))
    stale = False
    if card.get("status") == "claimed":
        stale = lease.is_stale(
            card, lock, config["heartbeat_stale_seconds"], now
        )

    budget = card.get("budget") or {}
    stats = read_run_stats(board, card_id)
    diff = (
        worktree.diffstat(board, card_id)
        if card.get("status") in ("needs_review", "claimed")
        else ""
    )

    starting = dispatch.is_starting(card, now)

    # The commit hook is the first line; this is the one that cannot be
    # sidestepped, because it reads what the branch actually contains.
    violations = []
    allow, deny = scope.card_scope(card)
    if diff and deny:
        report = scope.check_paths(worktree.branch_paths(board, card_id), allow, deny)
        violations = [
            {"path": path, "pattern": pattern}
            for path, pattern in report[scope.DENIED]
        ]

    # Only ever link to GitHub over https: this lands in an href.
    pr_url = artifacts.get("pr")
    pr_url = pr_url if str(pr_url or "").startswith("https://") else None
    pr_checked = pr.is_pull_url(pr_url) and pr.gh_bin() is not None

    return {
        "id": card_id,
        "title": card.get("title") or "(untitled)",
        "status": card.get("status"),
        "tier": card.get("tier"),
        "read_only": bool(card.get("read_only")),
        "origin": card.get("origin"),
        "parent": card.get("parent"),
        "depends_on": card.get("depends_on") or [],
        "blocked_by": [
            {"id": dep, "status": status} for dep, status in blocked
        ],
        "goal": spec.get("goal") or "",
        "acceptance": spec.get("acceptance") or [],
        "scope_allow": spec.get("scope_allow") or [],
        "scope_deny": spec.get("scope_deny") or [],
        "skills": spec.get("skills") or [],
        "owner": lease_info.get("owner"),
        "claimed_age": _age(lease_info.get("claimed_at"), now),
        "heartbeat_age": _age(lease_info.get("heartbeat_at"), now),
        "stale": stale,
        "starting": starting,
        "attempts": card.get("attempts") or 0,
        "max_attempts": config["max_attempts"],
        "spent_turns": stats.get("turns", budget.get("spent_turns") or 0),
        "cost_usd": stats.get("cost_usd"),
        "review_reason": review.get("reason"),
        "question": review.get("question"),
        "worktree": artifacts.get("worktree"),
        "branch": artifacts.get("branch"),
        "notes": preamble,
        "plan": sections.get(model.SECTION_PLAN, ""),
        "context": sections.get(model.SECTION_CONTEXT, ""),
        "feedback": sections.get(model.SECTION_FEEDBACK, ""),
        "log": sections.get(model.SECTION_LOG, ""),
        "diffstat": diff,
        "scope_violations": violations,
        "pr": pr_url,
        "agent_result": stats.get("result", ""),
        "asking": asking(card, bool(diff), pr_url),
        "actions": actions_for(
            card, blocked, dispatch_available, has_changes=bool(diff),
            starting=starting, violations=violations,
            pr_url=pr_url, pr_checked=pr_checked,
        ),
    }


def board_payload(board, dispatch_available=True):
    """Everything the dashboard needs for one render."""
    from . import __version__

    now = datetime.datetime.now(datetime.timezone.utc)
    config = board.read_config()

    cards = []
    broken = []
    for card_id, card, body in board.all_cards():
        if card is None:
            broken.append({"id": card_id, "error": body})
            continue
        cards.append(
            card_view(board, card_id, card, body, now, config, dispatch_available)
        )

    by_status = {key: [] for key, _ in COLUMNS}
    for view in cards:
        by_status.setdefault(view["status"], []).append(view)
    for bucket in by_status.values():
        bucket.sort(key=lambda view: view["id"])

    running = len(by_status.get("claimed", []))
    starting = sum(1 for view in cards if view["starting"])
    stale_count = sum(1 for view in cards if view["stale"])
    total_cost = sum(
        view["cost_usd"] or 0
        for view in cards
        if view["status"] != "merged"
    )

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": board.root,
        "project": os.path.basename(board.root),
        "version": __version__,
        "schema_version": config.get("schema_version"),
        "columns": [
            {"key": key, "label": label, "cards": by_status.get(key, [])}
            for key, label in COLUMNS
        ],
        "unreadable": broken,
        "stats": {
            "running": running,
            "starting": starting,
            "max_concurrent": config["max_concurrent_agents"],
            "queued": len(by_status.get("ready", [])) - starting,
            "awaiting_review": len(by_status.get("needs_review", [])),
            "stale": stale_count,
            "cost_usd": round(total_cost, 4) if total_cost else 0,
        },
        "dispatch_available": dispatch_available,
        "available_skills": sorted(available_skills(board.root)),
    }


def available_skills(root):
    """Skill names this project can actually invoke."""
    found = set()
    for base in (
        os.path.join(root, ".claude", "skills"),
        os.path.join(os.path.expanduser("~"), ".claude", "skills"),
    ):
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if os.path.isfile(os.path.join(base, name, "SKILL.md")):
                found.add(name)
    return found
