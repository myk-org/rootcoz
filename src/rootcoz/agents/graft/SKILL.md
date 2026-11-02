---
name: rootcoz-graft
description: Use rootcoz's read-only code graph to explore the cloned repositories before reading source files. It answers where code lives, who calls what, and what a file's interface is, from an index instead of file-by-file reading.
---

# Rootcoz code graph

The clones in this job are indexed. Querying the graph costs a few hundred
tokens; rebuilding the same picture by reading file after file costs thousands
and still misses the call edges. So: **graph first, source second** — use the
graph to find the exact `file:line`, then `read` only those lines.

These are the only supported tools. They are read-only, scoped to the clones of
this job, and never run shell commands. Always pass the intended repository name
as `repo` when more than one clone is available.

## Tools

### `graft_find_code` — locate behaviour (the default)
Ranked search for where something happens: "where is DHCP mode handled", "what
builds the request pipeline", "who retries on timeout".
- Params: `query` (symptom/behaviour words), optional `limit` (1-20), optional `path` to narrow a subtree.
- **Use when** the question is conceptual or locational and you do not know the file.
- One focused query usually answers. Few or weak hits: switch tool, don't reword the same query.

### `graft_find_all` — exhaustive matches
Every indexed occurrence of a pattern (`--fixed` literal semantics), not ranked top-N.
- Params: `pattern`, optional `path`.
- **Use when** you need completeness: all call sites of a helper, all uses of a constant, every provider implementation. `graft_find_code` is ranked and *will* miss instances.

### `graft_file_api` — a file's interface without its body
Signature-level view of one file: functions, classes, parameters.
- Params: `path` (repo-relative, must be indexed).
- **Use when** you know the file and need its shape before deciding what to read — cheaper than reading a 900-line module.

### `graft_trace_calls` — who calls / what it calls
Call edges for a symbol.
- Params: `symbol`, `direction` (`in` = callers, `out` = callees), `depth` (1-10).
- **Use when** judging blast radius or finding the entry point that reaches a failure: a stack-trace function is far more useful once you know its callers.

### `graft_repo_map` — orient in an unfamiliar clone
Directory/file groups with the busiest symbols per area.
- Params: optional `max_dirs` (1-30).
- **Use when** starting on a repository you have not looked at in this session, or when the failure names a component you cannot place.

### `graft_check_freshness` — is the index current
Reports whether the graph still matches the source.
- **Use when** a result looks wrong or empty before you conclude the code is absent.

## Rules

- Repository name goes in `repo`; never infer another workspace or host path. Absolute paths are rejected.
- Results may be truncated or partial. Check the returned status; on `stale`, `fallback`, `truncated`, or a failed call, use `read`, `ls`, `find`, `grep` for that file instead of re-querying.
- Do not try to build or refresh graphs yourself — indexing happens before the session starts.
- Never paste repository contents or credentials into a prompt.
