# 0012 — Fail-closed node process identity verification

Status: Accepted

## Context

`verify_process_identity()` previously accepted a live PID when platform-specific
lookups were incomplete. On POSIX it skipped executable verification, ignored
process start time, and did not require `instance_id` in the command line.

## Decision

- `node.lifecycle.json` includes a platform-verifiable `process_start_token`
  (Linux `/proc/<pid>/stat` field 22, Windows `GetProcessTimes` creation time).
- `verify_process_identity()` requires executable, command line, start token, and
  exact `--instance-id` matches whenever the platform exposes them.
- Missing required evidence fails closed with
  `Process identity could not be verified; refusing to signal PID ...`.
- `node.lifecycle.json` is validated against `schema/node-lifecycle.schema.json`
  before use.
- `node cleanup-stale` never removes lifecycle files from a verified live node;
  `--dangerous` applies only to live but unverified PIDs.

## Consequences

- PID reuse and forged lifecycle files cannot pass verification without matching
  executable, command line, start token, and instance id.
- Operators must use `node stop` for healthy nodes; `cleanup-stale` is for dead
  or explicitly reviewed unverified PIDs only.
