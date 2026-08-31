"""Deterministic double-entry ledger core.

This package is deliberately pure: no I/O, no network, no clock, no randomness.
Every effect the engine needs is injected as a Protocol (``Clock``, ``IdGenerator``,
``FxRateSource``) so that a replay of the same command sequence produces byte-identical
output. ``scripts/check_determinism.py`` enforces the ban in CI; see
``docs/adr/0001-architecture.md`` for the reasoning.
"""

from __future__ import annotations
