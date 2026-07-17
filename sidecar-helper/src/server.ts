// Point process.argv[1] at the real `pi` CLI so the subagent extension's
// getPiInvocation() spawns `node <pi-cli> ...` instead of re-running this
// sidecar (argv[1] would otherwise be dist/server.js).
//
// Clearing argv[1] falls through to `{ command: "pi" }`, but `pi` is not on
// PATH inside the container unless entrypoint adds node_modules/.bin.
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);

function resolvePiCli(): string {
  try {
    return require.resolve("@earendil-works/pi-coding-agent/dist/cli.js");
  } catch {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const candidate = path.resolve(
      here,
      "../node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
    );
    if (existsSync(candidate)) {
      return candidate;
    }
    throw new Error(
      "Cannot resolve pi CLI for subagent spawning; is @earendil-works/pi-coding-agent installed?",
    );
  }
}

process.argv[1] = resolvePiCli();

import { startSidecar } from "@myk-org/pi-sidecar";

startSidecar();
