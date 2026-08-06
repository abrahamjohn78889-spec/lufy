"""Notifications out. Nothing comes back in.

This package is the second component permitted to open a socket (the first is
arc/market/), and the permission is one-directional by construction: it sends and
never receives. There is no polling loop, no webhook and no command handler, because
a Telegram message that could arm trading would make the chat account a second set
of trading credentials held by whichever phone is signed in.
"""

from __future__ import annotations

__all__: list[str] = []
