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
  const cliModule = "@earendil-works/pi-coding-agent/dist/cli.js";
  try {
    return require.resolve(cliModule);
  } catch {
    // Prefer resolving relative to @myk-org/pi-sidecar — npm may nest
    // pi-coding-agent under that package instead of hoisting it.
    try {
      const sidecarRoot = path.dirname(
        require.resolve("@myk-org/pi-sidecar/package.json"),
      );
      return require.resolve(cliModule, { paths: [sidecarRoot] });
    } catch {
      // fall through
    }
    const here = path.dirname(fileURLToPath(import.meta.url));
    for (const candidate of [
      path.resolve(here, "../node_modules/@earendil-works/pi-coding-agent/dist/cli.js"),
      path.resolve(
        here,
        "../node_modules/@myk-org/pi-sidecar/node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
      ),
    ]) {
      if (existsSync(candidate)) {
        return candidate;
      }
    }
    throw new Error(
      "Cannot resolve pi CLI for subagent spawning; is @earendil-works/pi-coding-agent installed?",
    );
  }
}

process.argv[1] = resolvePiCli();

import { startSidecar } from "@myk-org/pi-sidecar";

startSidecar();
