"""A mutation fence prevents awaited extraction from resurrecting withdrawn sources."""
from contextvars import ContextVar

expected_revision = ContextVar('memory_expected_revision', default=None)


class StaleSource(ValueError):
    pass
