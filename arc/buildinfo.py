"""Which build is running, read from `.git` without shelling out.

`arc run` must never spawn a subprocess to answer a question about itself: a
`git` binary is not guaranteed on a VPS, and a subprocess that hangs would hang
the runtime while it was trying to describe itself. Both readers degrade to
UNAVAILABLE rather than raising, because a missing `.git` (a tarball deploy) is a
legitimate deployment and not a fault.

Lives outside `arc.api` because two callers need it — the Systems page and the
runtime session row — and the runtime must not import the dashboard's models to
learn its own commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = ["UNAVAILABLE", "git_branch", "git_commit"]

UNAVAILABLE: Final[str] = "UNAVAILABLE"

_GIT_DIR: Final[Path] = Path(__file__).resolve().parent.parent / ".git"

# Enough to identify a commit unambiguously in this repository while staying short
# enough to sit in a dashboard cell and a log prefix.
_SHORT_SHA_LENGTH: Final[int] = 12


def git_commit() -> str:
    """HEAD as a short sha, or UNAVAILABLE."""
    try:
        head = (_GIT_DIR / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            return (_GIT_DIR / head[5:]).read_text(encoding="utf-8").strip()[:_SHORT_SHA_LENGTH]
        # Detached HEAD: the file holds the sha itself.
        return head[:_SHORT_SHA_LENGTH]
    except OSError:
        return UNAVAILABLE


def git_branch() -> str:
    """The checked-out branch name, or UNAVAILABLE.

    A detached HEAD reports UNAVAILABLE rather than inventing a name: there is no
    branch, and reporting one would misdescribe the deployment.
    """
    try:
        head = (_GIT_DIR / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return UNAVAILABLE
    if not head.startswith("ref: "):
        return UNAVAILABLE
    return head[5:].removeprefix("refs/heads/")
