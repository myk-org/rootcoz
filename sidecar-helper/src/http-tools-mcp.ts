/**
 * Stdio MCP server that exposes sidecar HTTP custom tools to CLI/acpx agents.
 *
 * Tool list is the per-session dump written by rootcoz (analysis, job chat, or
 * admin chat). Re-read on every tools/list and tools/call so token refresh
 * does not require restarting the MCP process.
 */
import { readFileSync } from "node:fs";
import {
  createHttpToolExecutor,
  normalizeHttpToolConfig,
  type HttpToolConfig,
} from "@myk-org/pi-sidecar";

interface HttpToolDef {
  name: string;
  description?: string;
  parameters?: Record<string, unknown>;
  http?: Record<string, unknown>;
}

const PROTOCOL_VERSION = "2024-11-05";

function toolsFilePath(): string {
  const path = process.env.ROOTCOZ_HTTP_TOOLS_FILE?.trim();
  if (!path) {
    throw new Error("ROOTCOZ_HTTP_TOOLS_FILE is not set");
  }
  return path;
}

function loadTools(): HttpToolDef[] {
  const raw = readFileSync(toolsFilePath(), "utf8");
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) {
    return [];
  }
  return parsed.filter(
    (t): t is HttpToolDef =>
      typeof t === "object" &&
      t !== null &&
      typeof (t as HttpToolDef).name === "string" &&
      Boolean((t as HttpToolDef).http),
  );
}

function expandHttpConfig(
  http: Record<string, unknown>,
  params: Record<string, unknown>,
): HttpToolConfig {
  const qp = http.queryParams ?? http.query_params ?? http.query;
  const expanded = { ...http };
  if (qp === true) {
    expanded.queryParams = Object.fromEntries(
      Object.keys(params).map((key) => [key, `{${key}}`]),
    );
    delete expanded.query_params;
    delete expanded.query;
  } else if (qp && typeof qp === "object") {
    expanded.queryParams = qp;
  }
  return normalizeHttpToolConfig(expanded);
}

function send(message: Record<string, unknown>): void {
  const json = JSON.stringify(message);
  const payload = Buffer.from(json, "utf8");
  process.stdout.write(`Content-Length: ${payload.length}\r\n\r\n`);
  process.stdout.write(payload);
}

function respond(
  id: string | number | null,
  result: unknown,
): void {
  send({ jsonrpc: "2.0", id, result });
}

function respondError(
  id: string | number | null,
  code: number,
  message: string,
): void {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

async function handleRequest(msg: {
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}): Promise<void> {
  const id = msg.id ?? null;
  const method = msg.method ?? "";
  if (msg.id === undefined && method.startsWith("notifications/")) {
    return;
  }

  if (method === "initialize") {
    respond(id, {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "rootcoz-http-tools", version: "1.0.0" },
    });
    return;
  }

  if (method === "ping" || method === "notifications/initialized") {
    if (msg.id !== undefined) {
      respond(id, {});
    }
    return;
  }

  if (method === "tools/list") {
    const tools = loadTools().map((tool) => ({
      name: tool.name,
      description: tool.description ?? "",
      inputSchema: tool.parameters ?? { type: "object", properties: {} },
    }));
    respond(id, { tools });
    return;
  }

  if (method === "tools/call") {
    const params = msg.params ?? {};
    const name = params.name;
    if (typeof name !== "string" || !name) {
      respondError(id, -32602, "tools/call requires params.name");
      return;
    }
    const args =
      params.arguments && typeof params.arguments === "object"
        ? (params.arguments as Record<string, unknown>)
        : {};
    const tool = loadTools().find((t) => t.name === name);
    if (!tool?.http) {
      respondError(id, -32602, `Unknown tool: ${name}`);
      return;
    }
    const httpConfig = expandHttpConfig(tool.http, args);
    const executor = createHttpToolExecutor(httpConfig);
    const text = await executor(args);
    respond(id, {
      content: [{ type: "text", text }],
      isError: text.startsWith("HTTP request failed:") || /^HTTP \d{3}:/.test(text),
    });
    return;
  }

  if (msg.id !== undefined) {
    respondError(id, -32601, `Method not found: ${method}`);
  }
}

function dispatch(raw: string): void {
  let msg: { id?: string | number | null; method?: string; params?: Record<string, unknown> };
  try {
    msg = JSON.parse(raw) as typeof msg;
  } catch {
    return;
  }
  void handleRequest(msg).catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    if (msg.id !== undefined) {
      respondError(msg.id ?? null, -32603, message);
    }
  });
}

function readStdin(): void {
  let buffer = Buffer.alloc(0);
  process.stdin.on("data", (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (true) {
      const headerEnd = buffer.indexOf("\r\n\r\n");
      if (headerEnd === -1) {
        const ndjsonNl = buffer.indexOf("\n");
        if (ndjsonNl === -1) {
          break;
        }
        const line = buffer.subarray(0, ndjsonNl).toString("utf8").trim();
        buffer = buffer.subarray(ndjsonNl + 1);
        if (line.startsWith("{")) {
          dispatch(line);
        }
        continue;
      }
      const header = buffer.subarray(0, headerEnd).toString("utf8");
      const match = header.match(/Content-Length:\s*(\d+)/i);
      if (!match) {
        buffer = buffer.subarray(headerEnd + 4);
        continue;
      }
      const length = Number(match[1]);
      const bodyStart = headerEnd + 4;
      if (buffer.length < bodyStart + length) {
        break;
      }
      const body = buffer.subarray(bodyStart, bodyStart + length).toString("utf8");
      buffer = buffer.subarray(bodyStart + length);
      dispatch(body);
    }
  });
}

readStdin();
process.stdin.resume();
