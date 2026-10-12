"""Agent Kanban Board -- a file-backed board whose workers are AI agents.

One file per card, status as a field, atomic claiming via ``O_EXCL`` locks.
Stdlib only, so a board works in any project folder with no install step.
"""

__version__ = "0.1.0"
