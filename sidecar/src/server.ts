import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { SessionStore } from "./sessions.js";
import { startWatchdog } from "./watchdog.js";

const PORT = parseInt(process.env.SIDECAR_PORT || "9100", 10);
const HOST = process.env.DEV_MODE === "true" ? "0.0.0.0" : "127.0.0.1";
const PYTHON_HEALTH_URL = `http://localhost:${process.env.PORT || "8000"}/health`;

const store = new SessionStore();

// Simple JSON body parser
async function parseBody(req: IncomingMessage): Promise<any> {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk: Buffer) => { body += chunk.toString(); });
    req.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (e) {
        reject(new Error("Invalid JSON body"));
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res: ServerResponse, status: number, data: any): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

function routeMatch(url: string, pattern: string): Record<string, string> | null {
  // Simple pattern matching: /sessions/:id/prompt
  const patternParts = pattern.split("/");
  const urlParts = url.split("?")[0].split("/");
  if (patternParts.length !== urlParts.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < patternParts.length; i++) {
    if (patternParts[i].startsWith(":")) {
      params[patternParts[i].slice(1)] = urlParts[i];
    } else if (patternParts[i] !== urlParts[i]) {
      return null;
    }
  }
  return params;
}

const server = createServer(async (req, res) => {
  const method = req.method || "GET";
  const url = req.url || "/";

  try {
    // GET /health
    if (method === "GET" && url === "/health") {
      if (!store.ready) {
        sendJson(res, 503, { status: "starting", message: "Model discovery in progress" });
        return;
      }
      sendJson(res, 200, { status: "ok", sessions: store.count() });
      return;
    }

    // GET /models
    if (method === "GET" && url === "/models") {
      sendJson(res, 200, { models: store.getModels() });
      return;
    }

    // POST /models/refresh
    if (method === "POST" && url === "/models/refresh") {
      const models = await store.refreshModels();
      sendJson(res, 200, { models });
      return;
    }

    // POST /sessions
    if (method === "POST" && url === "/sessions") {
      const body = await parseBody(req);
      const { provider, model, system_prompt, cwd } = body;
      if (!provider || !system_prompt) {
        sendJson(res, 400, { error: "provider and system_prompt are required" });
        return;
      }
      const sessionId = await store.create({
        provider,
        model: model || "",
        systemPrompt: system_prompt,
        cwd: cwd || process.cwd(),
      });
      sendJson(res, 201, { session_id: sessionId });
      return;
    }

    // POST /sessions/:id/prompt
    let params = routeMatch(url, "/sessions/:id/prompt");
    if (method === "POST" && params) {
      const body = await parseBody(req);
      if (!body.message) {
        sendJson(res, 400, { error: "message is required" });
        return;
      }
      const result = await store.prompt(params.id, body.message);
      sendJson(res, 200, result);
      return;
    }

    // POST /sessions/:id/abort
    params = routeMatch(url, "/sessions/:id/abort");
    if (method === "POST" && params) {
      await store.abort(params.id);
      sendJson(res, 200, { aborted: true });
      return;
    }

    // DELETE /sessions/:id
    params = routeMatch(url, "/sessions/:id");
    if (method === "DELETE" && params) {
      store.delete(params.id);
      sendJson(res, 200, { deleted: true });
      return;
    }

    sendJson(res, 404, { error: "Not found" });
  } catch (err: any) {
    const message = err?.message || "Internal server error";
    const status = message.includes("not found") ? 404 : 500;
    console.error(`[sidecar] ${method} ${url} error:`, message);
    sendJson(res, status, { error: message });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[sidecar] Pi SDK sidecar listening on http://${HOST}:${PORT}`);
  startWatchdog(PYTHON_HEALTH_URL, () => {
    console.log("[sidecar] Python backend unresponsive, shutting down");
    process.exit(1);
  });

  // Auto-discover models from extensions on startup
  store.refreshModels().catch((err) => {
    console.error("[sidecar] Model discovery failed:", err);
  });
});

// Stale session cleanup every 10 minutes
setInterval(() => {
  store.cleanupStale(60 * 60 * 1000); // 1 hour
}, 10 * 60 * 1000);
