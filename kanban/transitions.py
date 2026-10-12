"""The state machine, and the only code allowed to write ``status``.

Invariant 7 says every state transition appends to the card body. The way that
is guaranteed rather than merely intended is that :func:`apply` is the single
chokepoint: it writes ``status``, appends the audit line, validates, and saves,
all together. Nothing else in the codebase may assign to ``card["status"]``.

Invariants 4 and 5 are enforced here too, via :data:`HUMAN_ONLY`.
"""

from . import locks, model
from .model import utcnow

#: Legal (from, to) pairs. Anything absent is refused.
ALLOWED = frozenset(
    (
        ("inbox", "ready"),
        ("ready", "claimed"),
        ("claimed", "ready"),
        ("claimed", "inbox"),
        ("claimed", "needs_review"),
        ("needs_review", "ready"),
        ("needs_review", "merged"),
        # A crash-looping card is parked for a human rather than respawned.
        ("ready", "needs_review"),
    )
)

#: Transitions only a human may perform (invariants 4 and 5).
HUMAN_ONLY = frozenset(
    (
        ("inbox", "ready"),
        ("claimed", "inbox"),
        ("needs_review", "ready"),
        ("needs_review", "merged"),
    )
)


class TransitionError(Exception):
    """Raised when a transition is illegal or not permitted for this actor."""


def check(from_status, to_status, actor):
    """Raise unless ``actor`` may move a card from one status to another."""
    if from_status == to_status:
        raise TransitionError("card is already %s" % to_status)
    if to_status not in model.STATUSES:
        raise TransitionError("unknown status %r" % (to_status,))
    if (from_status, to_status) not in ALLOWED:
        raise TransitionError(
            "illegal transition %s -> %s" % (from_status, to_status)
        )
    if (from_status, to_status) in HUMAN_ONLY and not actor.is_human:
        raise TransitionError(
            "%s -> %s is human-only; %s may not do it.\n"
            "       Use the dashboard -- this is the review gate, and it is "
            "deliberate." % (from_status, to_status, actor.name)
        )


def apply(board, card_id, card, body, to_status, actor, detail=""):
    """Move a card to ``to_status``, appending the audit line. Saves the card.

    Callers set any other fields (lease, review, tier) on ``card`` *before*
    calling; this function reads the old status from the card itself.

    This is also where the claim lock is dropped. Keeping that here rather than
    in each verb is what makes "a lock exists iff status is claimed" true by
    construction: no future transition can forget to release it.
    """
    from_status = card.get("status")
    check(from_status, to_status, actor)

    # Callers load, decide, then save. If another process moved the card in
    # between -- the sweeper reaping while the agent submits -- saving would
    # silently undo that transition. This narrows the window to the instant
    # between this read and the save below; it does not close it entirely.
    on_disk = board.status_on_disk(card_id)
    if on_disk is not None and on_disk != from_status:
        raise TransitionError(
            "card %s changed to %s while %s -> %s was in progress; "
            "reload and try again" % (card_id, on_disk, from_status, to_status)
        )

    card["status"] = to_status
    if to_status != "claimed":
        card["lease"] = None
    if actor.is_human and to_status == "ready":
        # The loop guards stand in for a human throttle. A human re-queuing
        # the card *is* that throttle, so it gets a fresh set of tries --
        # otherwise answering a parked card re-parked it instantly.
        card["attempts"] = 0
        card["dispatch_failures"] = 0
    body = model.append_log(body, actor.name, from_status, to_status, detail)
    model.require_valid(card)
    board.save(card_id, card, body)

    if from_status == "claimed" and to_status != "claimed":
        locks.release(board.lock_path(card_id))
    return card, body


# --------------------------------------------------------------------------
# dependency gating
# --------------------------------------------------------------------------

def blocking_deps(board, card):
    """Return [(dep_id, status)] for dependencies that are not yet merged.

    A missing dependency counts as blocking and is reported as ``"missing"``:
    a typo in ``depends_on`` should stop the card, not be silently ignored.
    """
    blockers = []
    for dep_id in card.get("depends_on") or []:
        dep_id = str(dep_id)
        try:
            dep, _ = board.load(dep_id)
        except Exception:
            blockers.append((dep_id, "missing"))
            continue
        if dep.get("status") != "merged":
            blockers.append((dep_id, dep.get("status")))
    return blockers


def describe_blockers(blockers):
    return ", ".join("%s (%s)" % (dep, status) for dep, status in blockers)


# --------------------------------------------------------------------------
# the verbs
# --------------------------------------------------------------------------

def promote(board, card_id, actor, detail=""):
    """inbox -> ready. Human only (invariant 4)."""
    card, body = board.load(card_id)
    return apply(board, card_id, card, body, "ready", actor, detail)


def submit_question(board, card_id, actor, question):
    """claimed -> needs_review, asking the human something."""
    card, body = board.load(card_id)
    card["review"] = {
        "reason": "question",
        "question": question,
        "submitted_at": utcnow(),
    }
    return apply(
        board, card_id, card, body, "needs_review", actor, "question"
    )


def submit_plan(board, card_id, actor, plan_text):
    """claimed -> needs_review with a plan for approval."""
    card, body = board.load(card_id)
    if plan_text:
        body = model.set_section(body, model.SECTION_PLAN, plan_text)
    card["review"] = {"reason": "plan", "submitted_at": utcnow()}
    return apply(board, card_id, card, body, "needs_review", actor, "plan")


def submit_work(board, card_id, actor, detail=""):
    """claimed -> needs_review with finished work."""
    card, body = board.load(card_id)
    card["review"] = {"reason": "work", "submitted_at": utcnow()}
    return apply(
        board, card_id, card, body, "needs_review", actor, detail or "work"
    )


def answer(board, card_id, actor, text):
    """needs_review(question) -> ready, with the answer recorded. Human only."""
    card, body = board.load(card_id)
    review = card.get("review") or {}
    question = review.get("question") or ""
    body = model.append_to_section(
        body,
        model.SECTION_CONTEXT,
        "- %s  Q: %s\n- %s  A: %s" % (utcnow(), question, utcnow(), text.strip()),
    )
    card["review"] = None
    return apply(board, card_id, card, body, "ready", actor, "answered")


def approve(board, card_id, actor, detail=""):
    """Approve whatever is in review. Human only (invariant 5).

    A plan approval flips ``tier`` to ``act`` and sends the card back to
    ``ready`` for execution; approved work goes to ``merged``. Only
    ``reason == "work"`` can ever reach ``merged``.
    """
    card, body = board.load(card_id)
    review = card.get("review") or {}
    reason = review.get("reason")

    if reason == "plan":
        card["tier"] = "act"
        card["review"] = None
        return apply(
            board, card_id, card, body, "ready", actor, "plan approved"
        )

    if reason == "work":
        card["review"] = None
        result = apply(
            board, card_id, card, body, "merged", actor, detail or "approved"
        )
        # The change has landed, so the worktree has done its job. The branch
        # is kept -- it is the record of how the card was done.
        try:
            from . import worktree

            worktree.remove(board, card_id)
        except Exception:
            pass  # a leftover worktree is untidy, not a correctness problem
        return result

    raise TransitionError(
        "card %s is in review as %r; only a plan or finished work can be "
        "approved. Answer a question with `answer` instead." % (card_id, reason)
    )


def reject(board, card_id, actor, feedback):
    """needs_review -> ready with feedback. Human only (invariant 5).

    The worktree and branch are deliberately kept: review feedback usually
    means "fix this", so the next claimant continues the existing work.
    """
    if not (feedback or "").strip():
        raise TransitionError("rejection needs feedback -- say what is wrong")
    card, body = board.load(card_id)
    body = model.append_to_section(
        body,
        model.SECTION_FEEDBACK,
        "- %s  %s: %s" % (utcnow(), actor.name, feedback.strip()),
    )
    card["review"] = None
    return apply(board, card_id, card, body, "ready", actor, "rejected")


def to_inbox(board, card_id, actor, detail="moved back to inbox"):
    """claimed -> inbox: a human pulls a running card back. Human only.

    ``apply`` drops the lease and the lock. Stopping the agent process is the
    caller's job (see ``dispatch.stop_agent``); work so far stays on the card
    and its branch.
    """
    card, body = board.load(card_id)
    return apply(board, card_id, card, body, "inbox", actor, detail)


def park_for_review(board, card_id, actor, detail):
    """Send a card to review without a human, for automatic incidents.

    Used when a card exhausts ``max_attempts``: parking it is safer than
    respawning an agent that keeps dying, which would burn the rate limit.
    """
    card, body = board.load(card_id)
    card["review"] = {
        "reason": "question",
        "question": detail,
        "submitted_at": utcnow(),
    }
    return apply(board, card_id, card, body, "needs_review", actor, "parked")
