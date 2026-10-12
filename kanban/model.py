"""Card schema: shape, ordering, validation, and body sections.

``yamlite`` handles syntax; this module owns the schema. It knows the canonical
key order (so diffs stay readable), the legal enum values, and how the card
body is divided into sections.

The ordering contract matters for upgradeability: ``normalize`` emits known
keys in canonical order and then appends every key it does not recognise,
untouched. An older tool opening a card written by a newer one preserves the
fields it cannot interpret rather than deleting them.
"""

import datetime

from . import yamlite

SCHEMA_VERSION = 2

STATUSES = ("inbox", "ready", "claimed", "needs_review", "merged")
ORIGINS = ("human", "planner")
RUNTIMES = ("claude-code",)
TIERS = ("plan", "act")
REVIEW_REASONS = ("question", "plan", "work")

#: Statuses a human may move a card out of, and nothing else may.
HUMAN_ONLY_FROM = ("inbox", "needs_review")

TOP_ORDER = (
    "id",
    "title",
    "status",
    "origin",
    "parent",
    "depends_on",
    "spec",
    "runtime",
    "tier",
    "read_only",
    "lease",
    "budget",
    "artifacts",
    "review",
    "attempts",
    "dispatch_failures",
)

NESTED_ORDER = {
    "spec": ("goal", "acceptance", "scope_allow", "scope_deny", "skills"),
    "lease": ("owner", "claimed_at", "expires_at", "heartbeat_at"),
    "budget": ("max_turns", "spent_turns"),
    "artifacts": ("worktree", "branch", "transcript", "pr"),
    "review": ("reason", "question", "submitted_at"),
}

# Body sections, in the order they should appear.
SECTION_PLAN = "Plan"
SECTION_CONTEXT = "Context"
SECTION_FEEDBACK = "Feedback"
SECTION_LOG = "Log"
SECTION_ORDER = (SECTION_PLAN, SECTION_CONTEXT, SECTION_FEEDBACK, SECTION_LOG)


class CardError(ValueError):
    """Raised when a card is structurally invalid."""


def utcnow():
    """Timestamp in the format the spec's examples use."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_ts(text):
    """Parse a card timestamp, returning an aware datetime or None."""
    if not text:
        return None
    try:
        parsed = datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------

def new_card(
    card_id,
    title,
    goal="",
    acceptance=None,
    origin="human",
    tier="plan",
    read_only=False,
    parent=None,
    depends_on=None,
    scope_allow=None,
    scope_deny=None,
    skills=None,
    max_turns=40,
):
    """Build a fresh card dict in canonical order."""
    card = {
        "id": str(card_id),
        "title": title,
        "status": "inbox",
        "origin": origin,
        "depends_on": list(depends_on or []),
        "spec": {
            "goal": goal,
            "acceptance": list(acceptance or []),
            "scope_allow": list(scope_allow or []),
            "scope_deny": list(scope_deny or []),
            "skills": list(skills or []),
        },
        "runtime": "claude-code",
        "tier": tier,
        "read_only": bool(read_only),
        "lease": None,
        "budget": {"max_turns": max_turns, "spent_turns": 0},
        "artifacts": {
            "worktree": None,
            "branch": None,
            "transcript": None,
            "pr": None,
        },
        "review": None,
        "attempts": 0,
        "dispatch_failures": 0,
    }
    if parent:
        card["parent"] = parent
    return normalize(card)


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------

def normalize(card):
    """Return ``card`` with keys in canonical order, unknown keys preserved.

    Unknown keys are appended after the known ones rather than dropped. This
    is what makes an older tool safe to run against a newer card.
    """
    return _order(card, TOP_ORDER, NESTED_ORDER)


def _order(data, order, nested):
    if not isinstance(data, dict):
        return data
    out = {}
    for key in order:
        if key in data:
            value = data[key]
            if key in nested:
                value = _order(value, nested[key], {})
            out[key] = value
    for key, value in data.items():
        if key not in out:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(card):
    """Return a list of human-readable problems; empty means valid."""
    problems = []

    if not str(card.get("id") or "").strip():
        problems.append("id is missing")
    elif not isinstance(card.get("id"), str):
        problems.append(
            "id must be a string (got %r) -- quote it so 042 stays 042"
            % (card["id"],)
        )

    if not str(card.get("title") or "").strip():
        problems.append("title is missing")

    status = card.get("status")
    if status not in STATUSES:
        problems.append(
            "status %r is not one of %s" % (status, "|".join(STATUSES))
        )

    if card.get("origin") not in ORIGINS:
        problems.append(
            "origin %r is not one of %s" % (card.get("origin"), "|".join(ORIGINS))
        )

    if card.get("runtime") not in RUNTIMES:
        problems.append(
            "runtime %r is not one of %s"
            % (card.get("runtime"), "|".join(RUNTIMES))
        )

    if card.get("tier") not in TIERS:
        problems.append(
            "tier %r is not one of %s" % (card.get("tier"), "|".join(TIERS))
        )

    spec = card.get("spec")
    if not isinstance(spec, dict):
        problems.append("spec is missing or not a mapping")
    else:
        for key in ("acceptance", "scope_allow", "scope_deny", "skills"):
            if key in spec and spec[key] is not None:
                if not isinstance(spec[key], list):
                    problems.append("spec.%s must be a list" % key)

    if card.get("depends_on") is not None and not isinstance(
        card.get("depends_on"), list
    ):
        problems.append("depends_on must be a list")

    review = card.get("review")
    if status == "needs_review":
        if not isinstance(review, dict):
            problems.append("status is needs_review but review is missing")
        elif review.get("reason") not in REVIEW_REASONS:
            problems.append(
                "review.reason %r is not one of %s"
                % (review.get("reason"), "|".join(REVIEW_REASONS))
            )
        elif review["reason"] == "question" and not review.get("question"):
            problems.append("review.reason is question but no question is set")

    lease = card.get("lease")
    if status == "claimed":
        if not isinstance(lease, dict):
            problems.append("status is claimed but lease is missing")
        elif not lease.get("owner"):
            problems.append("lease has no owner")

    attempts = card.get("attempts")
    if attempts is not None and not isinstance(attempts, int):
        problems.append("attempts must be an integer")

    return problems


def require_valid(card):
    problems = validate(card)
    if problems:
        raise CardError(
            "card %s is invalid:\n  - %s"
            % (card.get("id", "?"), "\n  - ".join(problems))
        )


# --------------------------------------------------------------------------
# body sections
# --------------------------------------------------------------------------

def split_sections(body):
    """Split a body into (preamble, {heading: text}, [heading order]).

    Only top-level ``## `` headings are treated as sections, so a ``### ``
    inside an agent's plan does not accidentally start a new one.
    """
    preamble_lines = []
    sections = {}
    order = []
    current = None
    buf = []

    for line in body.splitlines():
        if line.startswith("## "):
            if current is None:
                pass
            else:
                sections[current] = "\n".join(buf).strip("\n")
            current = line[3:].strip()
            if current not in order:
                order.append(current)
            buf = []
        elif current is None:
            preamble_lines.append(line)
        else:
            buf.append(line)

    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")

    return "\n".join(preamble_lines).strip("\n"), sections, order


def join_sections(preamble, sections, order=None):
    """Rebuild a body, placing known sections in canonical order."""
    order = list(order or sections.keys())
    known = [name for name in SECTION_ORDER if name in sections]
    extra = [name for name in order if name not in known and name in sections]
    parts = []
    if preamble.strip():
        parts.append(preamble.strip("\n"))
    for name in extra + known:
        text = sections.get(name, "")
        parts.append("## %s\n\n%s" % (name, text.strip("\n")) if text.strip() else "## %s" % name)
    return "\n\n".join(parts).strip("\n") + "\n"


def get_section(body, heading):
    _, sections, _ = split_sections(body)
    return sections.get(heading, "")


def set_section(body, heading, text):
    preamble, sections, order = split_sections(body)
    sections[heading] = text.strip("\n")
    if heading not in order:
        order.append(heading)
    return join_sections(preamble, sections, order)


def append_to_section(body, heading, text):
    preamble, sections, order = split_sections(body)
    existing = sections.get(heading, "").strip("\n")
    sections[heading] = (existing + "\n" + text.strip("\n")).strip("\n")
    if heading not in order:
        order.append(heading)
    return join_sections(preamble, sections, order)


def append_log(body, actor, from_status, to_status, detail=""):
    """Append one audit line. Invariant 7's only implementation.

    The arrow is intentional and matches the spec's notation; all card I/O is
    explicit UTF-8 so it survives on a cp1252 Windows console.
    """
    line = "- %s  %s  %s → %s" % (utcnow(), actor, from_status, to_status)
    if detail:
        line += "  (%s)" % detail
    return append_to_section(body, SECTION_LOG, line)


def append_note(body, actor, text):
    """Append a dated entry to the Context section."""
    entry = "- %s  %s: %s" % (utcnow(), actor, text.strip())
    return append_to_section(body, SECTION_CONTEXT, entry)


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------

def loads(text):
    """Parse a card document into (card dict, body)."""
    card, body = yamlite.parse_document(text)
    if not card:
        raise CardError("card has no frontmatter")
    return card, body


def dumps(card, body):
    """Render a card document with canonical key order."""
    return yamlite.format_document(normalize(card), body)
