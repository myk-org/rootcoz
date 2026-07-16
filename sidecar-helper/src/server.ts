// Clear process.argv[1] so the subagent extension's getPiInvocation() falls
// through to `{ command: "pi", args }` instead of re-running the sidecar.
process.argv[1] = "";

import { startSidecar } from "@myk-org/pi-sidecar";

startSidecar();
