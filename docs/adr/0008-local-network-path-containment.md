# 0008 — Local network registry path containment

Status: Accepted

## Context

`local-network.json` records the node directories created by
`network run-local`. An earlier format stored absolute `data_dir` paths and
copied live `pid`/`running` fields into the registry. A tampered registry could
point `network stop-local` at directories outside the requested network root,
causing stop files to be written, PID files removed, or unrelated processes
signaled.

## Decision

- The registry stores only **relative node names** (`node1`, `node2`, …) and an
  advisory `port`; it never stores absolute paths or PID values.
- Names must match `^node[1-9][0-9]*$`. Each resolved path is
  `(network_root / name).resolve()` and must satisfy `relative_to(network_root)`.
- Legacy entries containing `data_dir` are rejected.
- `network status` and `network stop-local` read PID/state only from the
  validated node directory (`node.pid`, `config.json`), never from registry
  fields.
- When `config.json` exists, its `data_dir` must resolve to the same path as
  the contained node directory.

## Consequences

- Registry files are treated as untrusted input; path traversal and symlink
  escapes are rejected before any stop/status side effects.
- Operators must re-run `network run-local` to regenerate registries in the new
  name-only format; hand-edited absolute-path registries fail fast.
- Status output still reports resolved absolute paths for display, but those
  paths are derived from the trusted root plus validated names.
