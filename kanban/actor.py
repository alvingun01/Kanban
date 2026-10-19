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
ENV_CARD = "KANBAN_CARD"  # set by dispatch on every agent it starts


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
    def from_env(cls, card_id=None):
        """The CLI's actor: an agent unless ``KANBAN_HUMAN=1`` is set.

        Without ``KANBAN_ACTOR`` an agent is named after the card it is acting
        on, the same name dispatch gives it. Naming it after the working
        directory broke the brief's own loop: claim from the project root,
        ``cd`` into the worktree, and the heartbeat came from a different name
        and was refused.
        """
        name = os.environ.get(ENV_ACTOR) or (
            "claude-code:%s" % card_id if card_id else default_agent_name()
        )
        # Every dispatched agent carries KANBAN_CARD, and so does every command
        # it runs. An agent typing `KANBAN_HUMAN=1 kanban approve` inline would
        # otherwise pass the allowlist as a plain `kanban` call and approve
        # its own work. Getting rid of KANBAN_CARD takes `unset` or `env -u`,
        # which the allowlist does not include.
        human = os.environ.get(ENV_HUMAN) == "1" and ENV_CARD not in os.environ
        return cls(name, human=human)

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
