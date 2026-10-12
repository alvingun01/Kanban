"""Who is acting, and whether they count as human.

Invariants 4 and 5 exist so agents cannot promote or merge their own work. On
a single-user laptop that cannot rest on OS identity -- Claude Code runs as the
same user -- so the interface is the boundary instead:

* The **dashboard** acts as a human. It constructs :meth:`Actor.as_human`
  directly, in-process, only after checking the session cookie.
* The **CLI** acts as an agent. It has no cookie and cannot grant itself
  humanity except through ``KANBAN_HUMAN``, which exists for tests and
  scripting.

``KANBAN_HUMAN`` *is* the trust boundary. Never set it anywhere an agent can
read it, and never put it in a skill or a checked-in settings file.
"""

import getpass
import os
import socket

ENV_ACTOR = "KANBAN_ACTOR"
ENV_HUMAN = "KANBAN_HUMAN"


class Actor:
    """An identity plus whether it may perform human-only transitions."""

    def __init__(self, name, human=False):
        self.name = name
        self.is_human = bool(human)

    def __repr__(self):
        return "Actor(%r, human=%r)" % (self.name, self.is_human)

    def __eq__(self, other):
        return (
            isinstance(other, Actor)
            and other.name == self.name
            and other.is_human == self.is_human
        )

    @classmethod
    def from_env(cls):
        """The CLI's actor: an agent unless ``KANBAN_HUMAN=1`` is set."""
        name = os.environ.get(ENV_ACTOR) or default_agent_name()
        return cls(name, human=os.environ.get(ENV_HUMAN) == "1")

    @classmethod
    def as_human(cls, name=None):
        """The dashboard's actor, after the session cookie has been checked."""
        return cls(name or default_human_name(), human=True)

    @classmethod
    def system(cls):
        """The lease sweeper and other automatic maintenance."""
        return cls("kanban:sweeper", human=False)


def default_human_name():
    try:
        user = getpass.getuser()
    except Exception:
        user = "you"
    return "%s@%s" % (user, short_hostname())


def default_agent_name():
    """Name an agent by the worktree it is running in, when it is in one."""
    leaf = os.path.basename(os.path.abspath(os.getcwd()))
    return "claude-code:%s" % (leaf or "?")


def short_hostname():
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return "localhost"
