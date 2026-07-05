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
  node is ready, and on any failure stops started nodes and removes temporary
  or incomplete registry files.

## Consequences

- Filesystem errors during startup no longer leave orphaned node processes
  without a registry or stale PID/lock files without a running node.
- Operators can treat `node.ready.json` as the readiness signal for automation.
