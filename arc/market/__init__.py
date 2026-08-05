"""Market engine: discovery, PTB, feeds, validation, staleness, rotation.

This is the only package that talks to the network. The domain layer, the storage
layer and the configuration layer import nothing that opens a socket, which is
what keeps their behaviour reproducible from a test without a venue.
"""

from __future__ import annotations
