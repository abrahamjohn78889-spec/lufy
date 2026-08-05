"""The Window Engine: does a frozen trigger hold?

Four modules, one question each:

    lifecycle    which state transitions are legal
    activation   has a window's activation instant passed (LEVEL check)
    freeze       lock five values atomically, or change nothing
    evaluate     has the direction-appropriate comparison passed

This layer decides NOTHING about trading. It produces no intent, sizes no order,
touches no wallet and reads no book. A window that fires is a window whose frozen
comparison came true; what happens next belongs to engines that do not exist yet.
"""

from __future__ import annotations
