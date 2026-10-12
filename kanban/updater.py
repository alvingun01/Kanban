"""Installing a patch, and migrating every board afterwards.

Two channels, because one of them cannot be assumed to exist:

* ``pull()`` -- if this tool is a git clone with a remote, fetch and fast-forward.
* ``apply_patch(source)`` -- take a directory or zip containing a ``kanban/``
  package and swap it in. This is the channel that works with no network and no
  remote, which is the common case for a tool you were handed as a folder.

A patch is validated in a temp directory *before* it replaces anything: it must
import and its CLI must run. The old package is kept alongside as
``kanban.old-<version>`` so a bad patch can be reversed by hand.

Nothing here touches a card. Cards live in project folders; the tool lives
somewhere else, and that separation is the first line of the preservation
guarantee.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from . import __version__

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(TOOL_ROOT, "kanban")


class UpdateError(Exception):
    """The patch was not applied; the tool is unchanged."""


def _run(args, cwd):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def has_remote():
    result = _run(["git", "remote"], TOOL_ROOT)
    return result.returncode == 0 and bool(result.stdout.strip())


def pull():
    """Fast-forward the tool from its git remote. Returns a status string."""
    if not os.path.isdir(os.path.join(TOOL_ROOT, ".git")):
        return "not a git clone -- use `kanban update --from <zip|dir>`"
    if not has_remote():
        return "no git remote configured -- use `kanban update --from <zip|dir>`"

    fetch = _run(["git", "fetch", "--quiet"], TOOL_ROOT)
    if fetch.returncode != 0:
        raise UpdateError("git fetch failed: %s" % fetch.stderr.strip())

    behind = _run(["git", "rev-list", "--count", "HEAD..@{u}"], TOOL_ROOT)
    if behind.returncode != 0:
        return "no upstream branch to compare against"
    if behind.stdout.strip() == "0":
        return "already up to date"

    merge = _run(["git", "merge", "--ff-only", "@{u}"], TOOL_ROOT)
    if merge.returncode != 0:
        raise UpdateError(
            "could not fast-forward (local changes?): %s" % merge.stderr.strip()
        )
    return "updated %s commit(s) from the remote" % behind.stdout.strip()


def _package_version(package_dir):
    init = os.path.join(package_dir, "__init__.py")
    try:
        with open(init, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1) if match else None


def _extract(source, workdir):
    """Return the path to a ``kanban`` package inside ``source``."""
    if os.path.isdir(source):
        staged = os.path.join(workdir, "src")
        shutil.copytree(source, staged)
    elif zipfile.is_zipfile(source):
        staged = os.path.join(workdir, "src")
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                # Refuse absolute paths and traversal before writing anything.
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                    raise UpdateError(
                        "patch contains an unsafe path (%s); refusing" % name
                    )
            archive.extractall(staged)
    else:
        raise UpdateError("%s is neither a directory nor a zip file" % source)

    if os.path.isfile(os.path.join(staged, "__init__.py")):
        return staged
    candidate = os.path.join(staged, "kanban")
    if os.path.isfile(os.path.join(candidate, "__init__.py")):
        return candidate
    for entry in sorted(os.listdir(staged)):
        nested = os.path.join(staged, entry, "kanban")
        if os.path.isfile(os.path.join(nested, "__init__.py")):
            return nested
    raise UpdateError("could not find a kanban package inside %s" % source)


def _verify(package_dir):
    """Run the candidate before trusting it."""
    parent = os.path.dirname(package_dir)
    if os.path.basename(package_dir) != "kanban":
        renamed = os.path.join(parent, "kanban")
        if os.path.exists(renamed):
            shutil.rmtree(renamed, ignore_errors=True)
        os.rename(package_dir, renamed)
        package_dir = renamed

    env = dict(os.environ)
    env["PYTHONPATH"] = parent
    env.pop("KANBAN_HUMAN", None)
    result = subprocess.run(
        [sys.executable or "python", "-m", "kanban", "--help"],
        cwd=parent, capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise UpdateError(
            "the patch does not run, so it was not installed:\n%s"
            % (result.stderr or result.stdout).strip()[:800]
        )
    return package_dir


def apply_patch(source):
    """Replace the installed package from ``source``. Returns a status string."""
    workdir = tempfile.mkdtemp(prefix="kanban-patch-")
    try:
        candidate = _extract(source, workdir)
        new_version = _package_version(candidate) or "unknown"
        candidate = _verify(candidate)

        retired = os.path.join(
            TOOL_ROOT, "kanban.old-%s" % (__version__ or "prev")
        )
        shutil.rmtree(retired, ignore_errors=True)

        # Swap by rename so the live package is only ever replaced by a
        # directory that is already complete and verified on disk.
        staged_final = os.path.join(TOOL_ROOT, "kanban.incoming")
        shutil.rmtree(staged_final, ignore_errors=True)
        shutil.copytree(candidate, staged_final)
        try:
            if os.path.isdir(PACKAGE_DIR):
                os.rename(PACKAGE_DIR, retired)
            os.rename(staged_final, PACKAGE_DIR)
        except OSError as exc:
            if os.path.isdir(retired) and not os.path.isdir(PACKAGE_DIR):
                os.rename(retired, PACKAGE_DIR)
            raise UpdateError("could not swap the package in: %s" % exc)

        return "installed v%s (previous kept at %s)" % (
            new_version, os.path.basename(retired)
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def update(source=None):
    """Install a patch, then report which boards need migrating."""
    from . import scaffold, store
    from .model import SCHEMA_VERSION

    if source:
        status = apply_patch(source)
    else:
        status = pull()

    needs_upgrade = []
    for root in scaffold.registered_boards():
        try:
            version = int(store.Board(root).read_config().get("schema_version", 1))
        except Exception:
            continue
        if version != SCHEMA_VERSION:
            needs_upgrade.append((root, version))

    return {
        "status": status,
        "version": __version__,
        "needs_upgrade": needs_upgrade,
    }
