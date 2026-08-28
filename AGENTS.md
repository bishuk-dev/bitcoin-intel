# Engineering Instructions

- Inspect the repository, tests, configuration, and relevant documentation before changing code.
- Implement only the requested phase; do not add speculative future modules or placeholder services.
- Preserve the monorepo architecture and prefer the smallest coherent change.
- Avoid new dependencies when the standard library or an existing dependency is sufficient.
- The deployed system must not require cloud services or runtime internet access.
- Keep Linux/Ubuntu compatibility; use `pathlib` rather than hardcoded path separators in Python.
- Keep validation and typing strong. Never weaken either silently to make a check pass.
- Do not leave fake implementations, disconnected abstractions, critical TODOs, or swallowed errors.
- Run the relevant formatter, lint, type, test, and build checks after changes.
- Never claim a command passed unless it was actually executed successfully.
- Preserve user work and avoid unrelated refactors or formatting changes.
- Canonical Bitcoin monetary values are integer satoshis; never round binary floating-point input.
- Canonical data-contract changes must be backward-compatible or explicitly schema-versioned.
- Never silently discard malformed source records or overwrite a conflicting TXID definition.
- Parquet remains canonical; any DuckDB state must be rebuildable from Parquet.
- Aggregate one-to-many relations independently before joining analytical summaries.
- Preserve integer satoshi correctness in analytical queries and aggregates.
