"""Board layout, config, atomic file writes, and id allocation.

Every write goes through :func:`atomic_write`, which writes a sibling temp file
and then ``os.replace``s it into place. That matters twice over: agents on the
same machine read cards concurrently, and this tree may sit inside a syncing
folder that can touch a file mid-write.

All card I/O is explicit UTF-8 with ``\\n`` newlines. Without the newline
pledge, Python on Windows would emit CRLF and a card body would stop being
byte-identical across platforms -- which is exactly the guarantee migrations
depend on.
"""

import json
import os
import re
import shutil
import tempfile
import time

from . import model
from .model import SCHEMA_VERSION

BOARD_DIR = ".kanban"
CONFIG_NAME = "config.json"
CARDS_DIR = "cards"
RUNS_DIR = "runs"
WORKTREE_DIR = "wt"
BACKUPS_DIR = "backups"

DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "lease_ttl_seconds": 1800,
    "heartbeat_stale_seconds": 600,
    "base_branch": "main",
    "port": 7777,
    "max_concurrent_agents": 2,
    "max_attempts": 3,
    # Permission mode for spawned agents, paired with dispatch.ALLOWED_TOOLS.
    # The allowlist is what makes an agent able to work; the mode stays narrow.
    "permission_mode": "acceptEdits",
    # Extra tool patterns a card agent may run here, e.g. "Bash(pytest *)".
    # Empty by default: widening this is a per-board decision you can see.
    "extra_allowed_tools": [],
    # null means "resolve at runtime" -- see dispatch.resolve_claude_bin. The
    # CLI bundled with the VSCode extension lives in a version-pinned path, so
    # hardcoding it here would break on every extension update.
    "claude_bin": None,
    "model": None,
}


class BoardError(Exception):
    """Raised for board-level problems: not found, version skew, bad id."""


class SchemaTooNew(BoardError):
    """The board was written by a newer tool than this one."""


class SchemaTooOld(BoardError):
    """The board needs migrating before this tool can use it."""


def _rmtree(path):
    """Remove a tree, clearing read-only bits, tolerating Windows quirks.

    Used only for scratch and already-retired directories, so a residual
    leftover is acceptable -- never for anything a caller still depends on.
    """
    if not os.path.isdir(path):
        return

    def _clear_readonly(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_clear_readonly)


def atomic_write(path, text):
    """Write ``text`` to ``path`` atomically, as UTF-8 with LF newlines.

    The temp file is unique per call. A fixed name meant two writers to the
    same card (an agent and the dashboard) truncated each other's temp file,
    and on Windows the slower ``os.replace`` then failed outright.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        _replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _replace(source, target, attempts=10):
    """``os.replace``, retrying briefly on Windows sharing violations.

    A reader, a virus scanner or a sync client holding ``target`` open makes
    Windows refuse the rename with ``PermissionError`` for a few milliseconds.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


#: A card id becomes a file name, a directory and a git branch, so it is held
#: to what is safe as all three. ``kanban new --id ../x`` used to write
#: outside ``cards/``, and the dashboard would load any ``.md`` it was named.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def check_id(card_id):
    """Return ``card_id`` as a string, or raise BoardError if it is unsafe."""
    text = str(card_id)
    if not _ID_RE.match(text) or ".." in text:
        raise BoardError(
            "invalid card id %r -- use letters, digits, '-', '_' or '.'" % text
        )
    return text


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class Board:
    """A single project's board directory."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    # -- discovery ------------------------------------------------------

    @classmethod
    def find(cls, start=None):
        """Walk up from ``start`` looking for a ``.kanban`` directory.

        A card worktree at ``<root>/wt/<id>`` is a checkout of the project, so
        once the board is committed it carries its own stale ``.kanban/`` and
        ``cards/``. Stopping there sent an agent's heartbeat, notes and submit
        to that copy instead of the live board, so a board found directly
        inside ``wt/`` defers to the one that owns the worktree.
        """
        current = os.path.abspath(start or os.getcwd())
        while True:
            if os.path.isfile(os.path.join(current, BOARD_DIR, CONFIG_NAME)):
                parent = os.path.dirname(current)
                owner = os.path.dirname(parent)
                if os.path.basename(parent) == WORKTREE_DIR and os.path.isfile(
                    os.path.join(owner, BOARD_DIR, CONFIG_NAME)
                ):
                    return cls(owner)
                return cls(current)
            parent = os.path.dirname(current)
            if parent == current:
                raise BoardError(
                    "no kanban board here (or in any parent directory)\n"
                    "       run: kanban init"
                )
            current = parent

    @classmethod
    def create(cls, root, **overrides):
        """Initialise a board, leaving an existing config untouched."""
        board = cls(root)
        os.makedirs(board.board_dir, exist_ok=True)
        os.makedirs(board.cards_dir, exist_ok=True)
        os.makedirs(board.runs_dir, exist_ok=True)
        if not os.path.isfile(board.config_path):
            config = dict(DEFAULT_CONFIG)
            config.update(overrides)
            board.write_config(config)
        return board

    # -- paths ----------------------------------------------------------

    @property
    def board_dir(self):
        return os.path.join(self.root, BOARD_DIR)

    @property
    def config_path(self):
        return os.path.join(self.board_dir, CONFIG_NAME)

    @property
    def cards_dir(self):
        return os.path.join(self.root, CARDS_DIR)

    @property
    def runs_dir(self):
        return os.path.join(self.root, RUNS_DIR)

    @property
    def worktrees_dir(self):
        return os.path.join(self.root, WORKTREE_DIR)

    @property
    def backups_dir(self):
        return os.path.join(self.board_dir, BACKUPS_DIR)

    def card_path(self, card_id):
        return os.path.join(self.cards_dir, "%s.md" % check_id(card_id))

    def lock_path(self, card_id):
        return os.path.join(self.cards_dir, "%s.lock" % check_id(card_id))

    def run_path(self, card_id):
        return os.path.join(self.runs_dir, "%s.jsonl" % check_id(card_id))

    def worktree_path(self, card_id):
        return os.path.join(self.worktrees_dir, check_id(card_id))

    def branch_name(self, card_id):
        return "card/%s" % check_id(card_id)

    # -- config ---------------------------------------------------------

    def read_config(self):
        """Load config, filling defaults but preserving unknown keys."""
        try:
            raw = json.loads(read_text(self.config_path))
        except FileNotFoundError:
            raise BoardError("board config missing: %s" % self.config_path)
        except ValueError as exc:
            raise BoardError("board config is not valid JSON: %s" % exc)
        config = dict(DEFAULT_CONFIG)
        config.update(raw)
        return config

    def write_config(self, config):
        atomic_write(self.config_path, json.dumps(config, indent=2) + "\n")

    def check_compat(self):
        """Refuse to operate on a board this tool cannot safely handle."""
        version = self.read_config().get("schema_version", 1)
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise BoardError("schema_version %r is not a number" % (version,))

        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                "board schema is v%d, this kanban only understands v%d\n"
                "       the tool is older than the board; update it:\n"
                "       run: kanban update" % (version, SCHEMA_VERSION)
            )
        if version < SCHEMA_VERSION:
            raise SchemaTooOld(
                "board schema is v%d, this kanban is v%d\n"
                "       run: kanban upgrade" % (version, SCHEMA_VERSION)
            )
        return version

    # -- cards ----------------------------------------------------------

    def card_ids(self):
        if not os.path.isdir(self.cards_dir):
            return []
        names = [
            name[:-3]
            for name in os.listdir(self.cards_dir)
            if name.endswith(".md")
        ]
        return sorted(names)

    def load(self, card_id):
        """Return (card dict, body). Raises BoardError if absent."""
        path = self.card_path(card_id)
        try:
            text = read_text(path)
        except FileNotFoundError:
            raise BoardError("no such card: %s" % card_id)
        card, body = model.loads(text)
        return card, body

    def status_on_disk(self, card_id):
        """The card's status as currently saved, or None if unreadable."""
        try:
            return self.load(card_id)[0].get("status")
        except (BoardError, model.CardError, ValueError):
            return None

    def save(self, card_id, card, body):
        atomic_write(self.card_path(card_id), model.dumps(card, body))

    def all_cards(self):
        """Yield (id, card, body) for every readable card.

        A card that fails to parse is reported rather than crashing a listing,
        so one bad file cannot make the whole board unusable.
        """
        for card_id in self.card_ids():
            try:
                card, body = self.load(card_id)
            except (BoardError, model.CardError, ValueError) as exc:
                yield card_id, None, str(exc)
                continue
            yield card_id, card, body

    def next_id(self):
        """Lowest unused zero-padded id.

        Collisions are still possible between two processes calling this at the
        same instant; the caller closes that window with an ``O_EXCL`` create.
        """
        used = set(self.card_ids())
        number = 1
        while True:
            candidate = "%03d" % number
            if candidate not in used:
                return candidate
            number += 1

    def create_card_file(self, card, body):
        """Create a new card, failing loudly if the id is taken.

        Uses ``O_EXCL`` so two concurrent ``new`` calls cannot collide on an id.
        """
        card_id = card["id"]
        os.makedirs(self.cards_dir, exist_ok=True)
        path = self.card_path(card_id)
        text = model.dumps(card, body)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BoardError("card %s already exists" % card_id)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
                out.write(text)
        except BaseException:
            os.unlink(path)
            raise
        return card_id

    # -- backups --------------------------------------------------------

    def snapshot_cards(self, label):
        """Copy ``cards/`` into ``.kanban/backups/<label>/`` and return it."""
        target = os.path.join(self.backups_dir, label)
        if os.path.exists(target):
            shutil.rmtree(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copytree(self.cards_dir, target)
        return target

    def restore_cards(self, snapshot):
        """Restore ``cards/`` from a snapshot directory, wholesale.

        Staged as copy-then-rename rather than delete-then-copy. Two reasons,
        and the first is not theoretical -- it failed in testing:

        * On Windows ``rmtree`` can raise ``PermissionError`` on the directory
          itself even after its contents are gone, because deletes complete
          asynchronously. A restore that can fail halfway is worthless, since
          this is the code that has to work when a migration has gone wrong.
        * Renaming means the live ``cards/`` is only ever replaced by a
          directory that is already complete on disk.
        """
        if not os.path.isdir(snapshot):
            raise BoardError("no such snapshot: %s" % snapshot)

        staging = self.cards_dir + ".restoring"
        _rmtree(staging)
        shutil.copytree(snapshot, staging)

        retired = "%s.old-%d" % (self.cards_dir, os.getpid())
        _rmtree(retired)
        try:
            if os.path.isdir(self.cards_dir):
                os.rename(self.cards_dir, retired)
            os.rename(staging, self.cards_dir)
        except OSError as exc:
            # Put the original back rather than leaving no cards directory.
            if os.path.isdir(retired) and not os.path.isdir(self.cards_dir):
                os.rename(retired, self.cards_dir)
            raise BoardError("could not restore cards/: %s" % exc)

        _rmtree(retired)  # best effort; the restore has already succeeded

    def latest_snapshot(self):
        if not os.path.isdir(self.backups_dir):
            return None
        names = sorted(os.listdir(self.backups_dir))
        return os.path.join(self.backups_dir, names[-1]) if names else None
