# 0011 — Failure-atomic node and local-network startup

Status: Accepted

## Context

Node and local-network startup could leave stale lifecycle files or running
background processes when a later initialization step failed. In particular,
`local-network.json` was written after child startup in a separate step, and
`run_node_process()` wrote `node.pid` and `node.lock` before config validation
completed.

## Decision

- **`run_node_process()`** acquires the lock, writes `config.json` atomically,
  opens the store, writes lifecycle identity, writes `node.pid`, and only then
  writes `node.ready.json`. Any failure before readiness removes every file
  created during startup.
- **`start_node()`** waits for `node.ready.json` with a matching `instance_id`,
  not merely a live PID. Readiness timeouts verify child identity, request
  shutdown, and clean lifecycle files.
- **`run_local_network()`** writes `local-network.starting.json` before
  launching children, writes the final registry atomically only after every
  node is ready, and on any failure rolls back started children before removing
  temporary registry files.
- Rollback tracks parent-owned `Popen` handles for every child started in the
  current attempt. It calls `stop_node()` only for `running_verified` children;
  when identity verification refuses stop (`live_unverified`, `malformed`), it
  terminates the exact child via the parent handle.
- Rollback treats a child as live when its parent `Popen` has not exited or
  when `node_status().pid_is_live` reports a different live PID at the node
  path. It does **not** use `ProcessStatus.running` alone, because that flag is
  `True` only for verified ownership.
- If any child remains live after rollback, `local-network.starting.json` is
  rewritten as a recovery registry (node name, port, PID, instance id, lifecycle
  state, stop failure) and startup raises an explicit recovery error. The final
  `local-network.json` is absent until startup completes successfully.
- Temporary registries are removed only after every spawned child is confirmed
  dead.

## Consequences

- Filesystem errors during startup no longer leave orphaned node processes
  without a registry or stale PID/lock files without a running node.
- A failed local-network startup cannot silently delete both registries while
  leaving a live child; operators retain recovery metadata in
  `local-network.starting.json` until every unresolved PID is handled.
- Operators can treat `node.ready.json` as the readiness signal for automation.
