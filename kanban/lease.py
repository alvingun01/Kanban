"""Claiming, releasing, heartbeats, and lease expiry.

Invariant 1 says claiming is atomic, and read-modify-write on the card file is
not sufficient. So the arbiter is an ``O_EXCL`` create of ``cards/<id>.lock``
(see :mod:`locks`): exactly one process can win that syscall. The card's
``lease:`` block mirrors the lock for human readability, but **the lock file is
authoritative**.

The claim sequence is deliberately ordered:

1. Take the lock (atomic; the loser fails here).
2. *Then* re-read the card and check it is still ``ready``.
3. On any refusal, drop the lock so the card is not left unclaimable.

Checking the status first would leave a window where two processes both saw
``ready`` and both proceeded.
"""

import datetime
import os

from . import locks, model, transitions, worktree
from .model import parse_ts, utcnow

#: Re-exported so callers can catch one exception type from this module.
ClaimConflict = locks.ClaimConflict


class ClaimRefused(Exception):
    """The card cannot be claimed right now (wrong status, blocked deps)."""


def read_lock(board, card_id):
    return locks.read(board.lock_path(card_id))


def claim(board, card_id, actor, force=False, ttl=None):
    """Claim a ready card. Returns (card, body).

    ``force`` overrides the ``depends_on`` gate and records the override in the
    card's audit log, so a forced claim stays visible afterwards.
    """
    config = board.read_config()
    ttl = ttl or config["lease_ttl_seconds"]
    lock_path = board.lock_path(card_id)

    # Step 1: win the lock. This single syscall decides the race.
    try:
        lock = locks.acquire(lock_path, actor.name, ttl)
    except locks.ClaimConflict as exc:
        raise locks.ClaimConflict("card %s is %s" % (card_id, exc))

    try:
        card, body = board.load(card_id)

        if card.get("status") != "ready":
            raise ClaimRefused(
                "card %s is %s, not ready" % (card_id, card.get("status"))
            )

        detail = ""
        blockers = transitions.blocking_deps(board, card)
        if blockers:
            described = transitions.describe_blockers(blockers)
            if not force:
                raise ClaimRefused(
                    "card %s is blocked\n"
                    "       depends_on %s is not merged\n"
                    "       claim anyway with: kanban claim %s --force"
                    % (card_id, described, card_id)
                )
            detail = "forced past unmerged deps: %s" % described

        card["lease"] = {
            "owner": actor.name,
            "claimed_at": lock["claimed_at"],
            "expires_at": lock["expires_at"],
            "heartbeat_at": lock["heartbeat_at"],
        }
        # setdefault is not enough here: the key exists with value None on a
        # fresh card, so it would never be filled in.
        artifacts = card.setdefault("artifacts", {}) or {}
        if not artifacts.get("transcript"):
            artifacts["transcript"] = "runs/%s.jsonl" % card_id

        # Create or reuse the card's worktree. A rejected card comes back to
        # the same branch, so the next agent keeps the earlier history.
        try:
            # A read-only card changes nothing, so it gets no branch either;
            # its agent reads the project as it currently is.
            path, branch = (
                (None, None) if card.get("read_only")
                else worktree.ensure(board, card_id)
            )
        except worktree.GitError as exc:
            path, branch = None, None
            body = model.append_to_section(
                body,
                model.SECTION_CONTEXT,
                "- %s  worktree unavailable: %s" % (utcnow(), exc),
            )
        if path:
            artifacts["worktree"] = os.path.relpath(path, board.root).replace(
                "\\", "/"
            )
            artifacts["branch"] = branch
        card["artifacts"] = artifacts
        card["attempts"] = (card.get("attempts") or 0) + 1
        # The agent got this far, so whatever was failing at launch is not
        # failing now. Clear the launch-failure counter.
        card["dispatch_failures"] = 0
        card.pop("dispatched_at", None)

        return transitions.apply(
            board, card_id, card, body, "claimed", actor, detail
        )
    except BaseException:
        # Never leave a lock behind on a refused claim.
        locks.release(lock_path)
        raise


def heartbeat(board, card_id, actor, ttl=None):
    """Refresh the lease so the sweeper does not reap a live agent."""
    config = board.read_config()
    ttl = ttl or config["lease_ttl_seconds"]

    card, body = board.load(card_id)
    if card.get("status") != "claimed":
        raise ClaimRefused(
            "card %s is %s, not claimed" % (card_id, card.get("status"))
        )

    lease = card.get("lease") or {}
    owner = lease.get("owner")
    if owner and owner != actor.name:
        raise ClaimRefused(
            "card %s is held by %s, not %s" % (card_id, owner, actor.name)
        )

    try:
        lock = locks.refresh(board.lock_path(card_id), ttl)
    except locks.ClaimConflict as exc:
        raise ClaimRefused("card %s: %s" % (card_id, exc))
    lease["heartbeat_at"] = lock["heartbeat_at"]
    lease["expires_at"] = lock["expires_at"]
    card["lease"] = lease
    # Saving a stale "claimed" over a card the sweeper just reaped would
    # leave it claimed with no lock. Check again right before writing.
    if board.status_on_disk(card_id) != "claimed":
        raise ClaimRefused("card %s was released while heartbeating" % card_id)
    board.save(card_id, card, body)
    return card, body


def release(board, card_id, actor, detail="released"):
    """Give a claimed card back to ``ready``. ``apply`` drops the lock."""
    card, body = board.load(card_id)
    if card.get("status") != "claimed":
        raise ClaimRefused(
            "card %s is %s, not claimed" % (card_id, card.get("status"))
        )
    return transitions.apply(board, card_id, card, body, "ready", actor, detail)


def is_stale(card, lock, heartbeat_stale_seconds, now=None):
    """True when a lease has both expired *and* gone quiet.

    Invariant 3 requires both conditions. Expiry alone would reap an agent
    working steadily through a long card; a stale heartbeat alone would reap
    one that simply has not checked in yet.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    source = lock or card.get("lease") or {}

    expires = parse_ts(source.get("expires_at"))
    if expires is None or expires > now:
        return False

    beat = parse_ts(source.get("heartbeat_at")) or parse_ts(
        source.get("claimed_at")
    )
    if beat is None:
        return True
    return (now - beat).total_seconds() >= heartbeat_stale_seconds


def sweep(board, actor=None, now=None):
    """Reap expired leases. Returns a list of (card_id, outcome) pairs.

    A card that has exhausted ``max_attempts`` is parked in review rather than
    returned to ``ready``: respawning an agent that keeps dying would burn the
    rate-limit window for nothing.
    """
    from .actor import Actor

    actor = actor or Actor.system()
    config = board.read_config()
    stale_after = config["heartbeat_stale_seconds"]
    max_attempts = config["max_attempts"]
    reaped = []

    for card_id, card, body in board.all_cards():
        if card is None or card.get("status") != "claimed":
            continue
        if not is_stale(card, read_lock(board, card_id), stale_after, now):
            continue

        owner = (card.get("lease") or {}).get("owner", "?")
        incident = "lease expired; owner %s went quiet" % owner
        body = model.append_to_section(
            body,
            model.SECTION_CONTEXT,
            "- %s  incident: %s" % (utcnow(), incident),
        )
        attempts = card.get("attempts") or 0

        if attempts >= max_attempts:
            card["review"] = {
                "reason": "question",
                "question": (
                    "This card has been attempted %d times and keeps failing "
                    "(%s). It needs a human look before another agent tries."
                    % (attempts, incident)
                ),
                "submitted_at": utcnow(),
            }
            to_status, detail = "needs_review", "parked after %d attempts" % attempts
            outcome = "parked"
        else:
            to_status, detail = "ready", incident
            outcome = "released"
        try:
            transitions.apply(board, card_id, card, body, to_status, actor, detail)
        except transitions.TransitionError:
            continue  # the agent submitted or released while we looked

        reaped.append((card_id, outcome))

    return reaped


def audit_locks(board):
    """Report cards where lock presence disagrees with status.

    The rule is "a lock exists iff status is claimed". Anything else means a
    crash landed the board in an inconsistent state, and ``kanban check``
    surfaces it rather than letting it sit.
    """
    problems = []
    for card_id, card, _ in board.all_cards():
        if card is None:
            continue
        has_lock = locks.read(board.lock_path(card_id)) is not None
        claimed = card.get("status") == "claimed"
        if claimed and not has_lock:
            problems.append("%s is claimed but has no lock file" % card_id)
        elif has_lock and not claimed:
            problems.append(
                "%s has a stray lock file but is %s" % (card_id, card.get("status"))
            )
    return problems
