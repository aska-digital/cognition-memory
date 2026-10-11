# Cognition memory provider

Session-end durable memory for Hermes: one active provider. Facts are
regex-extracted from your messages at session end, stored in a local SQLite
queue and JSON log under one repo label.

- **Push is fixed to session end.** Turns accumulate; `on_session_end`
  extracts durable facts onto a SQLite FIFO queue. `sync_turn` persists
  nothing. `Pending` means unpushed (extracted, not yet committed).
- **Fetch-first merge, keep-both conflicts.** Push compares against committed
  state first; a changed base keeps both versions under deterministic refs
  (`cog-{sessionId}-{seq}`, conflicts `{ref}-conflict-{unix_ts}`). Exact
  redelivery of an already-committed entry is idempotent.
- **Mandatory provenance.** Every entry carries `source_session`, `author`,
  `created_at`, `origin_repo`, `trace_id`. `archive_and_evict` archives an
  entry to JSONL before removing it; nothing in this version calls it
  automatically.
- **Single-writer lock per entry id.** Dream reads committed state only.
- **Prefetch budget** (default 8000 tokens). Truncation order: system-pin >
  provenance > recency > salience, always with a marker.
- **Dream is read-only.** `cognition_dream` synthesizes committed state and
  surfaces conflicts; no write-back path exists by design.

Config (DRAFT key names): `memory.provider=cognition` with
`memory.cognition={repos, budget_tokens, queue, archive}`. Setup asks for the
author identity last — the configured author is authoritative for provenance;
empty falls back to host `git config user.name`.

Recall injects committed facts under the token budget. Built-in memory
(`MEMORY.md`, `USER.md`) is left to Hermes core and is not mirrored here.

## Layout

- `plugin.yaml` — manifest; `__init__.py` — `register(ctx)`
- `provider.py` — `CognitionMemoryProvider` (tools: `cognition_search`,
  `cognition_status`, `cognition_dream`)
- `queue.py` — durable FIFO (retry 1m/5m/30m+jitter, 24h cap, bound 1000
  drop-oldest-with-tombstone, backlog surfacing)
- `merge.py` — fetch-first push, keep-both, per-entry locks, commit log.
  The on-disk `committed.json` is the sole truth: every push/remove runs
  refresh → compare → write as ONE transaction under cross-instance/process
  exclusion with the per-ref lock nested inside.
- `provenance.py` — mandatory provenance + eviction-preserving archive
- `budget.py` — prefetch budget + truncation order + marker
- `tests/` — one acceptance test per contract condition (`python3 -m
  unittest discover -s tests -t tests` from this directory). Tests load the
  real Hermes `agent.memory_provider` base (installed package, `PYTHONPATH`
  checkout, or `HERMES_AGENT_ROOT`); a missing dependency fails fast with a
  deterministic error — never a silent stub.
