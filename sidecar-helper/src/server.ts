// Point process.argv[1] at the real `pi` CLI so the subagent extension's
// getPiInvocation() spawns `node <pi-cli> ...` instead of re-running this
// sidecar (argv[1] would otherwise be dist/server.js).
//
// Clearing argv[1] falls through to `{ command: "pi" }`, but `pi` is not on
// PATH inside the container unless entrypoint adds node_modules/.bin.
import { createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";
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

// rootcoz requires pi-coding-agent >= 0.84.0 (http-tools MCP contract).
// pi-sidecar's own MIN_PI_VERSION floor may be lower, so enforce ours here.
const MIN_PI_VERSION = "0.84.0";

function findNearestManifest(startDir: string): string {
  // Walk up from a resolved file to the nearest package.json whose name is the
  // pi coding agent. Plain fs reads are used on purpose: requiring
  // "@earendil-works/pi-coding-agent/package.json" fails with
  // ERR_PACKAGE_PATH_NOT_EXPORTED because the package's "exports" map does
  // not expose "./package.json".
  let dir = path.dirname(startDir);
  while (dir !== path.dirname(dir)) {
    const candidate = path.join(dir, "package.json");
    if (existsSync(candidate)) {
      try {
        const manifest = JSON.parse(readFileSync(candidate, "utf8"));
        if (manifest.name === "@earendil-works/pi-coding-agent") {
          return candidate;
        }
      } catch {
        // Unreadable/invalid manifest — keep walking up.
      }
    }
    dir = path.dirname(dir);
  }
  throw new Error("no package.json for @earendil-works/pi-coding-agent found");
}

function assertPiVersion(): void {
  // resolvePiCli() already located the installed pi CLI (now process.argv[1]).
  // Walking up from that file sidesteps the package's "exports" map entirely:
  // both "pi-coding-agent/package.json" and the bare specifier are refused
  // under require() because the package is ESM-only (no CJS main exported).
  const manifestPath = findNearestManifest(process.argv[1]);
  const version: string = JSON.parse(readFileSync(manifestPath, "utf8")).version;
  const [maj, min] = version.split(".").map(Number);
  const [wantMaj, wantMin] = MIN_PI_VERSION.split(".").map(Number);
  if (maj < wantMaj || (maj === wantMaj && min < wantMin)) {
    throw new Error(
      `pi-coding-agent ${version} is too old; rootcoz requires >=${MIN_PI_VERSION}`,
    );
  }
  console.log(`[sidecar] pi-coding-agent ${version} >= ${MIN_PI_VERSION}: ok`);
}

assertPiVersion();

import { startSidecar } from "@myk-org/pi-sidecar";

startSidecar();
