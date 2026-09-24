---
name: rootcoz-graft
description: Use rootcoz's read-only indexed code graph to explore cloned repositories when graph tools are available.
---

# Rootcoz code graph

Only use the rootcoz-provided `graft_*` tools. They are read-only, scoped to cloned repositories for this job/session, and never require shell commands. Specify the intended `repo` scope when more than one clone is available. Never infer that another workspace or host path is accessible.

- `graft_repo_map`: orient yourself in an unfamiliar clone.
- `graft_find_code`: ranked search for where/how a behavior works; it is not an exhaustive occurrence list.
- `graft_find_all`: find every indexed match for a pattern (within the declared scope and output bounds).
- `graft_file_api`: inspect a file's signatures without reading full bodies.
- `graft_trace_calls`: trace callers (`in`) or callees (`out`) of a symbol.
- `graft_check_freshness`: check whether the graph still represents the cloned source.

Read the returned source locations with `read` when more detail is needed. Graph output may be truncated and indexing may skip files or entire repositories; check returned status, freshness, and truncation before claiming completeness. On unavailable, stale, partial, invalid, or timed-out graph results, use the existing `read`, `ls`, `find`, and `grep` tools instead. Do not try to build/refresh graphs yourself. Do not place repository contents or credentials in a prompt.
