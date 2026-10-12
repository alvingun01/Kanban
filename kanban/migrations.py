"""Card schema migrations.

Ordered, idempotent, and **frontmatter only**. A migration may reshape
frontmatter; it must never touch the card body. The body holds the agent
findings, the review history, and the audit log -- the parts no migration could
reconstruct -- so ``migrate_board`` asserts the body is unchanged and aborts if
any migration breaks that rule.

Safety around the whole operation:

* refuses to run on a dirty git tree, so git independently holds the old state
* snapshots ``cards/`` to ``.kanban/backups/<timestamp>/`` before writing
* validates every card afterwards and restores the snapshot wholesale if even
  one fails, so a bad migration is all-or-nothing
* never deletes or renames a card file; ``id`` and filename are immutable

Fixtures under ``tests/fixtures/board-vN/`` are added, never edited. That is
what stops the chain from rotting.
"""

import datetime
import os

from . import model, worktree
from .model import SCHEMA_VERSION


class MigrationError(Exception):
    """A migration could not be completed; the board was left unchanged."""


def migrate_1_to_2(card):
    """v1 (the spec as literally written) -> v2 (this tool's schema).

    Changes, all decided in the handoff conversation:

    * ``needs_input`` folded into ``needs_review`` with ``review.reason``
    * top-level ``question`` moved under ``review``
    * ``budget.max_usd``/``spent_usd`` replaced by turn counts -- a Claude
      subscription has no per-card dollar cost
    * ``runtime`` narrowed to ``claude-code``
    * ``spec.skills`` and ``attempts`` added

    Removed fields are carried through untouched rather than stripped: a field
    leaving the schema means it stops being *read*, not that it is deleted from
    someone's files.
    """
    card = dict(card)

    question = card.pop("question", None)
    status = card.get("status")

    if status == "needs_input":
        card["status"] = "needs_review"
        if not card.get("review"):
            card["review"] = {
                "reason": "question",
                "question": question or "(migrated from needs_input)",
                "submitted_at": model.utcnow(),
            }
    elif question and not card.get("review"):
        card["review"] = {
            "reason": "question",
            "question": question,
            "submitted_at": model.utcnow(),
        }
    card.setdefault("review", None)

    budget = dict(card.get("budget") or {})
    if "max_turns" not in budget:
        budget["max_turns"] = 40
    if "spent_turns" not in budget:
        budget["spent_turns"] = 0
    card["budget"] = budget

    if card.get("runtime") not in model.RUNTIMES:
        card["runtime"] = "claude-code"

    spec = dict(card.get("spec") or {})
    spec.setdefault("skills", [])
    card["spec"] = spec

    card.setdefault("attempts", 0)
    card.setdefault("dispatch_failures", 0)
    card.setdefault("tier", "act")
    card.setdefault("depends_on", [])
    card.setdefault("origin", "human")

    return card


#: version -> function taking it to version+1
MIGRATIONS = {1: migrate_1_to_2}


def plan_for(from_version):
    """The ordered list of migrations to reach the current schema."""
    steps = []
    version = int(from_version)
    while version < SCHEMA_VERSION:
        if version not in MIGRATIONS:
            raise MigrationError(
                "no migration from schema v%d; this board cannot be upgraded "
                "automatically" % version
            )
        steps.append((version, MIGRATIONS[version]))
        version += 1
    return steps


def migrate_card(card, from_version):
    """Apply every migration in turn. Returns the new card dict."""
    for _, func in plan_for(from_version):
        card = func(card)
    return model.normalize(card)


def describe_changes(before, after):
    """Summarise one card's frontmatter change, for ``--dry-run``."""
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if key not in before:
            changes.append("+ %s = %r" % (key, new))
        elif key not in after:
            changes.append("- %s (was %r)" % (key, old))
        else:
            changes.append("~ %s: %r -> %r" % (key, old, new))
    return changes


def migrate_board(board, dry_run=False, allow_dirty=False):
    """Migrate every card to the current schema. Returns a report.

    Either everything lands or nothing does.
    """
    config = board.read_config()
    from_version = int(config.get("schema_version", 1))

    if from_version > SCHEMA_VERSION:
        raise MigrationError(
            "board schema is v%d but this tool only understands v%d; "
            "update the tool rather than downgrading the board"
            % (from_version, SCHEMA_VERSION)
        )
    if from_version == SCHEMA_VERSION:
        return {"migrated": [], "from": from_version, "to": SCHEMA_VERSION,
                "snapshot": None, "already_current": True}

    steps = plan_for(from_version)

    if not dry_run and not allow_dirty and worktree.is_repo(board.root):
        if worktree.is_dirty(board.root):
            raise MigrationError(
                "the working tree has uncommitted changes.\n"
                "       Commit or stash first -- the migration is then a "
                "reviewable diff you can revert.\n"
                "       Override with --allow-dirty if you know what you are "
                "doing."
            )

    # Read everything up front so a parse failure aborts before any write.
    loaded = []
    for card_id in board.card_ids():
        try:
            card, body = board.load(card_id)
        except Exception as exc:
            raise MigrationError(
                "card %s cannot be read, so the board cannot be migrated "
                "safely: %s" % (card_id, exc)
            )
        loaded.append((card_id, card, body))

    report = {
        "from": from_version,
        "to": SCHEMA_VERSION,
        "steps": [version for version, _ in steps],
        "migrated": [],
        "snapshot": None,
        "already_current": False,
    }

    # Plan the whole batch before touching disk, and surface any failure as a
    # MigrationError so callers have exactly one exception type to handle. A
    # failure here means nothing was written, so there is nothing to restore.
    planned = []
    for card_id, card, body in loaded:
        try:
            after = migrate_card(dict(card), from_version)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                "migration of card %s failed before anything was written, so "
                "the board is unchanged.\n       cause: %s: %s"
                % (card_id, type(exc).__name__, exc)
            )
        planned.append((card_id, card, after, body))
        report["migrated"].append(
            {"id": card_id, "changes": describe_changes(card, after)}
        )

    if dry_run:
        return report

    label = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = board.snapshot_cards(label)
    report["snapshot"] = snapshot

    try:
        for card_id, _, after, body in planned:
            board.save(card_id, after, body)

        # Verify: every card must still parse, still validate, and -- the part
        # that matters most -- still have exactly the body it had before.
        for card_id, _, _, body_before in planned:
            card_after, body_after = board.load(card_id)
            if body_after != body_before:
                raise MigrationError(
                    "card %s body changed during migration. Migrations must "
                    "touch frontmatter only." % card_id
                )
            problems = model.validate(card_after)
            if problems:
                raise MigrationError(
                    "card %s is invalid after migration: %s"
                    % (card_id, "; ".join(problems))
                )

        config["schema_version"] = SCHEMA_VERSION
        board.write_config(config)
    except BaseException as exc:
        board.restore_cards(snapshot)
        raise MigrationError(
            "migration failed and the board was restored from %s\n"
            "       cause: %s" % (snapshot, exc)
        )

    return report


def rollback(board, snapshot=None):
    """Restore ``cards/`` from a snapshot, newest by default."""
    target = snapshot or board.latest_snapshot()
    if not target:
        raise MigrationError("no snapshot to roll back to")
    board.restore_cards(target)
    return target
