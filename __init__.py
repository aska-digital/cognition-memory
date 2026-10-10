"""Cognition memory provider — session-end durable push, fetch-first merge.

Single-active provider: facts regex-extracted from your messages at session
end, stored in a local SQLite queue and JSON log under one repo label, with
per-entry provenance and budgeted prefetch.
"""

from .provider import CognitionMemoryProvider, register  # noqa: F401
