# 0010 — Node lifecycle identity before stop signals

Status: Accepted

## Context

`node stop` previously trusted only the integer in `node.pid`. After a crash,
that file can outlive the Toychain process; the OS may later reuse the PID for
an unrelated program. Signaling such a PID is unsafe.

## Decision

- When a node starts, it writes `node.lifecycle.json` containing
  `schema_version`, `pid`, `instance_id`, `started_at`, resolved `data_dir`, and
  `executable`.
- The parent passes a unique `instance_id` to `_node-run`; the child writes the
  lifecycle record only after acquiring `node.lock`.
- `node stop` refuses to signal a live PID unless `verify_process_identity()`
  confirms executable and, where available, command-line markers for
  `toychain _node-run` and the expected data directory.
- Stale cleanup removes pid/lock/stop/lifecycle files only when the PID is dead
  or when the operator runs `node cleanup-stale --dangerous` after manual review
  of a live but unverified PID.

## Consequences

- Stop is fail-closed when identity cannot be proven.
- Operators may need `node cleanup-stale` after crashes that leave stale files
  with a reused PID; this is safer than signaling the wrong process.
