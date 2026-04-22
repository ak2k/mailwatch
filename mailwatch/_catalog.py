"""Shared invariant-check helper for catalog modules.

Both :mod:`mailwatch.layouts` and :mod:`mailwatch.avery` run an import-time
``_validate()`` pass over their catalog entries; they share the same need
for an assertion that survives ``python -O`` (plain ``assert`` is stripped).
"""

from __future__ import annotations


def check(cond: bool, msg: str) -> None:
    """Raise :class:`AssertionError` on invariant violation.

    Plain ``assert`` is stripped under ``python -O``; this form stays in so
    module import fails loudly no matter how the interpreter is invoked.
    """
    if not cond:
        raise AssertionError(msg)
