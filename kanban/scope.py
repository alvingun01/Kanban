"""Path matching for ``spec.scope_allow`` and ``spec.scope_deny``.

Invariant 6 says an agent that needs to touch a denied path stops and asks
rather than proceeding. This module decides what "denied" means; the
scope-guard commit hook is what makes it bite.

Patterns follow the shapes the spec uses -- ``src/auth/**``, ``.env*`` -- with
gitignore-like behaviour: a pattern containing no slash is also matched against
the basename, so ``.env*`` catches ``config/.env.local`` and not just a
top-level ``.env``.
"""

import re

ALLOWED = "allowed"
DENIED = "denied"
OUTSIDE = "outside"


def _to_regex(pattern):
    """Translate a glob into a regex, treating ``**`` as crossing separators."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 2] == "**":
                # Swallow a trailing slash so "src/**" also matches "src".
                if pattern[i:i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return "^" + "".join(out) + "$"


def normalise(path):
    """Normalise separators and strip a leading ``./`` -- and nothing more.

    Do not be tempted by ``lstrip("./")``: that strips a *set of characters*,
    so the pattern ``.env*`` becomes ``env*`` and stops matching dotfiles
    entirely. That bug let a denied ``.env`` through in testing.
    """
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def matches(path, pattern):
    """True when ``path`` matches the glob ``pattern``."""
    if not pattern:
        return False
    path = normalise(path)
    pattern = normalise(pattern)

    # "src/auth/**" should cover the directory itself, not only its contents.
    if pattern.endswith("/**") and (
        path == pattern[:-3] or path.startswith(pattern[:-3] + "/")
    ):
        return True

    regex = _to_regex(pattern)
    if re.match(regex, path):
        return True

    # gitignore-like: a slash-free pattern also applies to the basename.
    if "/" not in pattern:
        return bool(re.match(regex, path.rsplit("/", 1)[-1]))
    return False


def classify(path, allow=None, deny=None):
    """Return ALLOWED, DENIED, or OUTSIDE for one path.

    ``deny`` wins over ``allow``: an explicit denial is never overridden by a
    broader allow pattern. An empty ``allow`` list means "anything not denied",
    so a card need only state what it must not touch.
    """
    for pattern in deny or []:
        if matches(path, pattern):
            return DENIED
    if not allow:
        return ALLOWED
    for pattern in allow:
        if matches(path, pattern):
            return ALLOWED
    return OUTSIDE


def check_paths(paths, allow=None, deny=None):
    """Classify many paths. Returns {verdict: [(path, pattern or None)]}."""
    report = {DENIED: [], OUTSIDE: [], ALLOWED: []}
    for path in paths:
        verdict = classify(path, allow, deny)
        culprit = None
        if verdict == DENIED:
            culprit = next(
                (p for p in (deny or []) if matches(path, p)), None
            )
        report[verdict].append((path, culprit))
    return report


def card_scope(card):
    spec = card.get("spec") or {}
    return spec.get("scope_allow") or [], spec.get("scope_deny") or []
