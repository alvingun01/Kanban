"""Pull requests: how finished work leaves the board for review and merge.

When a card's work is submitted, the tool -- not the agent, which may not push
-- publishes ``card/<id>`` to the board's ``remote`` and opens a pull request
with the GitHub CLI (``gh``). You review and merge it on GitHub, then approve
the card on the dashboard. With ``gh`` installed, approval checks that the PR
really was merged; that is what keeps "Done" meaning the change has landed.

Without ``gh`` the branch is still pushed and the card links to GitHub's
"compare" page, which opens the PR in one click. Without a remote nothing here
runs, and approve works as it always has.
"""

import json
import os
import re
import shutil
import subprocess
from urllib.parse import quote

from . import worktree

_GITHUB_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


def gh_bin():
    return shutil.which("gh")


def github_slug(url):
    """``owner/repo`` for a GitHub remote URL, or None."""
    match = _GITHUB_RE.search(str(url or "").strip())
    return "%s/%s" % match.groups() if match else None


def compare_url(slug, base, branch):
    return "https://github.com/%s/compare/%s...%s?expand=1" % (
        slug, quote(base, safe=""), quote(branch, safe="")
    )


def _gh(gh, root, args):
    """Run gh; returns (ok, stdout, error text). Never raises."""
    try:
        result = subprocess.run(
            [gh] + args, cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=worktree.NETWORK_TIMEOUT,
            env=dict(os.environ, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    error = (result.stderr or result.stdout).strip().splitlines()
    return result.returncode == 0, result.stdout, error[0] if error else ""


def _body(card_id, card):
    spec = card.get("spec") or {}
    lines = ["Card %s from the kanban board." % card_id, ""]
    if spec.get("goal"):
        lines += ["**Goal**", "", spec["goal"], ""]
    if spec.get("acceptance"):
        lines += ["**Done when**", ""] + ["- %s" % item for item in spec["acceptance"]]
        lines.append("")
    lines.append("Merge it here, then approve the card on the dashboard.")
    return "\n".join(lines)


def _open_with_gh(gh, root, branch, base, card_id, card):
    """Find the open PR for ``branch``, or create one. Returns (url, error)."""
    ok, out, error = _gh(
        gh, root, ["pr", "list", "--head", branch, "--state", "open", "--json", "url"]
    )
    if ok:
        try:
            found = json.loads(out or "[]")
        except ValueError:
            found = []
        if found and found[0].get("url"):
            return found[0]["url"], None
    title = "%s: %s" % (card_id, card.get("title") or "card %s" % card_id)
    ok, out, error = _gh(
        gh, root,
        ["pr", "create", "--head", branch, "--base", base,
         "--title", title, "--body", _body(card_id, card)],
    )
    urls = [line.strip() for line in out.splitlines() if line.strip().startswith("https://")]
    if ok and urls:
        return urls[-1], None
    return None, "gh could not open the pull request (%s)" % (error or "no URL returned")


def publish(board, card_id, card):
    """Push the card's branch and open its pull request. Returns (url, note).

    ``url`` goes in ``artifacts.pr``; ``note``, when set, says what went wrong
    or what the human has to do instead, and belongs on the card. A failure
    here never blocks the submit: the work is committed either way, and the
    reviewer can see why there is no PR.
    """
    config = board.read_config()
    remote = config.get("remote")
    url = worktree.remote_url(board.root, remote)
    if not url:
        return None, None  # a local-only board: nothing to publish to

    branch = board.branch_name(card_id)
    base = config.get("base_branch") or "main"
    try:
        worktree.push(board.root, remote, branch)
    except worktree.GitError as exc:
        return None, "could not push %s to %s, so no pull request was opened: %s" % (
            branch, remote, exc)

    gh = gh_bin()
    error = "gh is not installed"
    if gh:
        pr_url, error = _open_with_gh(gh, board.root, branch, base, card_id, card)
        if pr_url:
            return pr_url, None

    slug = github_slug(url)
    if slug:
        return compare_url(slug, base, branch), (
            "pushed %s; %s, so the card links to GitHub's page for opening the "
            "pull request" % (branch, error))
    return None, "pushed %s to %s; %s, so open a pull request there by hand" % (
        branch, remote, error)


def is_pull_url(url):
    return bool(url) and "/pull/" in str(url)


def state(board, url):
    """"MERGED", "OPEN" or "CLOSED" for a real PR, or None when it cannot be told."""
    gh = gh_bin()
    if not gh or not is_pull_url(url):
        return None
    ok, out, _ = _gh(gh, board.root, ["pr", "view", url, "--json", "state"])
    if not ok:
        return None
    try:
        return str(json.loads(out).get("state") or "").upper() or None
    except (ValueError, AttributeError):
        return None
