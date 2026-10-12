"""The claim lock: an ``O_EXCL`` file that exactly one process can create.

This lives in its own module so that both :mod:`lease` (which takes the lock)
and :mod:`transitions` (which must drop it on every exit from ``claimed``) can
use it without a circular import.

**The invariant this module exists to support:** a lock file is present if and
only if the card's status is ``claimed``. Enforcing that inside
``transitions.apply`` rather than in each verb is deliberate -- the first draft
released the lock only in ``release()``, so ``submit_work`` leaked it and the
card became permanently unclaimable.
"""

import datetime
import json
import os


class ClaimConflict(Exception):
    """The lock already exists: someone else holds this card."""


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _iso_in(seconds):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=seconds
    )
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path):
    """Return the lock's contents, or None if absent or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return None


def acquire(path, owner, ttl_seconds):
    """Atomically create the lock. Raises :class:`ClaimConflict` if taken.

    ``O_CREAT | O_EXCL`` is the whole mechanism: the kernel guarantees exactly
    one caller creates the file, so the race is decided by a single syscall
    rather than by a read-then-write that two processes could interleave.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    now = _now_iso()
    payload = {
        "owner": owner,
        "pid": os.getpid(),
        "claimed_at": now,
        "expires_at": _iso_in(ttl_seconds),
        "heartbeat_at": now,
    }
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = read(path) or {}
        raise ClaimConflict(
            "already claimed by %s since %s"
            % (existing.get("owner", "?"), existing.get("claimed_at", "?"))
        )
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
        json.dump(payload, out, indent=2)
    return payload


def refresh(path, ttl_seconds):
    """Update the lock's heartbeat and expiry. Returns the new values.

    Raises :class:`ClaimConflict` if the lock is gone. Recreating it would
    resurrect a lease the sweeper already reaped, leaving a stray lock that
    makes the card unclaimable.
    """
    from .store import atomic_write

    lock = read(path)
    if lock is None:
        raise ClaimConflict("lock is gone; the lease was released or reaped")
    lock["heartbeat_at"] = _now_iso()
    lock["expires_at"] = _iso_in(ttl_seconds)
    atomic_write(path, json.dumps(lock, indent=2))
    return lock


def release(path):
    """Remove the lock. Returns True if one was there."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
