import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import {
  type AgentSession,
  AuthStorage,
  createAgentSession,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { getModel } from "@earendil-works/pi-ai";
import { createTools } from "./tools.js";

interface SessionEntry {
  session: AgentSession;
  lastActivity: number;
}

interface CreateSessionOptions {
  provider: string;
  model: string;
  systemPrompt: string;
  cwd: string;
  toolsConfig?: {
    serverUrl: string;
    authToken: string;
    jobId: string;
    jiraUrl?: string;
    jiraEmail?: string;
    jiraToken?: string;
    githubToken?: string;
    githubRepo?: string;
  };
}

// Extension paths — set during container build
const ACPX_EXTENSION = process.env.SIDECAR_ACPX_EXTENSION_PATH || "/app/sidecar/extensions/acpx-provider/index.ts";
const VERTEX_EXTENSION = process.env.SIDECAR_VERTEX_EXTENSION_PATH || "/app/sidecar/node_modules/pi-vertex-claude/index.ts";

export class SessionStore {
  private sessions = new Map<string, SessionEntry>();
  private authStorage = AuthStorage.create();
  private modelRegistry = ModelRegistry.create(this.authStorage);
  private _ready = false;
  private acpxModels: Array<{ id: string; name: string; provider: string }> = [];

  get ready(): boolean {
    return this._ready;
  }

  count(): number {
    return this.sessions.size;
  }

  getModels(): Array<{ id: string; name: string; provider: string }> {
    // Providers that require browser OAuth and cannot work in a headless container
    const HEADLESS_EXCLUDED_PROVIDERS = new Set(["github-copilot"]);

    const builtinModels = this.modelRegistry.getAvailable()
      .filter((m) => !HEADLESS_EXCLUDED_PROVIDERS.has(m.provider))
      .map((m) => ({
        id: m.id,
        name: m.name,
        provider: m.provider,
      }));

    return [...builtinModels, ...this.acpxModels];
  }

  /**
   * Discover models from all configured providers. Blocks until complete.
   * Called on startup — /health returns ok only after this finishes.
   */
  async refreshModels(): Promise<Array<{ id: string; name: string; provider: string }>> {
    // Create a bootstrap session to trigger extension loading.
    // Extensions like vertex-claude register models synchronously on load.
    // The session is kept alive (disposed at cleanup) to maintain extension state.
    const bootstrapId = await this.create({
      provider: "google",
      model: "gemini-2.5-flash",
      systemPrompt: "bootstrap",
      cwd: "/tmp",
    });
    console.log(`[sidecar] Bootstrap session created for extension loading`);

    // Discover acpx models for each agent in parallel (blocks until all complete)
    const agents = (process.env.ACPX_AGENTS || "")
      .split(",")
      .map((a) => a.trim())
      .filter((a) => a.length > 0);

    if (agents.length > 0) {
      console.log(`[sidecar] Discovering models for ACPX agents: ${agents.join(", ")}`);
      const results = await Promise.allSettled(
        agents.map((agent) => discoverAcpxModels(agent))
      );

      this.acpxModels = [];
      for (let i = 0; i < results.length; i++) {
        const result = results[i];
        const agent = agents[i];
        if (result.status === "fulfilled" && result.value.length > 0) {
          this.acpxModels.push(...result.value);
          console.log(`[sidecar] acpx-${agent}: ${result.value.length} models discovered`);
        } else if (result.status === "rejected") {
          console.error(`[sidecar] acpx-${agent}: discovery failed:`, result.reason);
        } else {
          console.warn(`[sidecar] acpx-${agent}: no models discovered`);
        }
      }
    }

    // Clean up bootstrap session
    this.delete(bootstrapId);

    this._ready = true;
    console.log(`[sidecar] Model discovery complete: ${this.getModels().length} models available`);
    return this.getModels();
  }

  async create(options: CreateSessionOptions): Promise<string> {
    const id = randomUUID();

    // Find the model
    let model = this.modelRegistry.find(options.provider, options.model) || undefined;
    if (!model) {
      // Try built-in models
      model = getModel(options.provider as any, options.model) || undefined;
    }

    // Build extension paths (only include existing files)
    const extensionPaths: string[] = [];
    try {
      const { accessSync } = await import("node:fs");
      accessSync(ACPX_EXTENSION);
      extensionPaths.push(ACPX_EXTENSION);
      console.log(`[sidecar] Extension found: ${ACPX_EXTENSION}`);
    } catch (err) {
      console.warn(`[sidecar] ACPX extension not found at ${ACPX_EXTENSION}:`, err);
    }
    try {
      const { accessSync } = await import("node:fs");
      accessSync(VERTEX_EXTENSION);
      extensionPaths.push(VERTEX_EXTENSION);
      console.log(`[sidecar] Extension found: ${VERTEX_EXTENSION}`);
    } catch (err) {
      console.warn(`[sidecar] Vertex extension not found at ${VERTEX_EXTENSION}:`, err);
    }
    console.log(`[sidecar] Loading ${extensionPaths.length} extensions`);

    // Build custom tools from config
    const customTools = options.toolsConfig ? createTools(options.toolsConfig) : [];

    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: false },
    });

    const loader = new DefaultResourceLoader({
      cwd: options.cwd,
      agentDir: "/tmp/rootcoz-sidecar-agent",
      settingsManager,
      additionalExtensionPaths: extensionPaths,
      systemPromptOverride: () => options.systemPrompt,
    });
    await loader.reload();

    const { session } = await createAgentSession({
      cwd: options.cwd,
      model,
      thinkingLevel: "off",
      tools: ["read", "grep", "find", "ls"],
      customTools,
      resourceLoader: loader,
      sessionManager: SessionManager.inMemory(),
      settingsManager,
      authStorage: this.authStorage,
      modelRegistry: this.modelRegistry,
    });

    this.sessions.set(id, { session, lastActivity: Date.now() });
    console.log(`[sidecar] Session created: ${id} (provider=${options.provider}, model=${options.model})`);
    return id;
  }

  async prompt(id: string, message: string): Promise<{ text: string; usage: any }> {
    const entry = this.sessions.get(id);
    if (!entry) throw new Error(`Session ${id} not found`);

    entry.lastActivity = Date.now();

    let responseText = "";
    const usage = { input_tokens: 0, output_tokens: 0, cost_usd: 0, duration_ms: 0 };
    const startTime = Date.now();

    const unsubscribe = entry.session.subscribe((event) => {
      if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
        responseText += event.assistantMessageEvent.delta;
      }
      if (event.type === "agent_end" && event.messages) {
        for (const msg of event.messages) {
          if (msg.role === "assistant" && msg.usage) {
            usage.input_tokens += msg.usage.input || 0;
            usage.output_tokens += msg.usage.output || 0;
            usage.cost_usd += msg.usage.cost?.total || 0;
          }
        }
      }
    });

    try {
      await entry.session.prompt(message);
    } finally {
      unsubscribe();
    }

    usage.duration_ms = Date.now() - startTime;

    return { text: responseText, usage };
  }

  async abort(id: string): Promise<void> {
    const entry = this.sessions.get(id);
    if (!entry) throw new Error(`Session ${id} not found`);
    await entry.session.abort();
  }

  delete(id: string): void {
    const entry = this.sessions.get(id);
    if (entry) {
      entry.session.dispose();
      this.sessions.delete(id);
      console.log(`[sidecar] Session deleted: ${id}`);
    }
  }

  cleanupStale(maxAge: number): void {
    const now = Date.now();
    for (const [id, entry] of this.sessions) {
      if (now - entry.lastActivity > maxAge) {
        console.log(`[sidecar] Cleaning up stale session: ${id}`);
        entry.session.dispose();
        this.sessions.delete(id);
      }
    }
  }
}

/**
 * Discover models from an acpx agent by spawning `acpx --model __list__ <agent> exec x`.
 * Blocks until the subprocess completes or times out (30s).
 */
function discoverAcpxModels(agent: string): Promise<Array<{ id: string; name: string; provider: string }>> {
  return new Promise((resolve, reject) => {
    const models: Array<{ id: string; name: string; provider: string }> = [];
    let output = "";
    let resolved = false;

    const proc = spawn("acpx", ["--model", "__list__", agent, "exec", "x"], {
      stdio: ["pipe", "pipe", "pipe"],
    });

    const timeout = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        proc.kill("SIGTERM");
        resolve(models);
      }
    }, 30000);

    proc.stderr.on("data", (chunk: Buffer) => { output += chunk.toString(); });
    proc.stdout.on("data", (chunk: Buffer) => { output += chunk.toString(); });

    proc.on("close", () => {
      if (resolved) return;
      resolved = true;
      clearTimeout(timeout);

      // Parse "Available models: modelId[opts], modelId2[opts], ..."
      const match = output.match(/Available models:\s*(.+)/);
      if (match) {
        const modelList = match[1].trim().replace(/\.$/, "");
        // Bracket-aware split: commas inside [] are part of the model ID
        const entries: string[] = [];
        let current = "";
        let depth = 0;
        for (const ch of modelList) {
          if (ch === "[") depth++;
          else if (ch === "]") depth = Math.max(0, depth - 1);
          if (ch === "," && depth === 0) {
            entries.push(current.trim());
            current = "";
          } else {
            current += ch;
          }
        }
        if (current.trim()) entries.push(current.trim());

        for (const entry of entries) {
          if (!entry) continue;
          const bracketIdx = entry.indexOf("[");
          const baseName = bracketIdx >= 0 ? entry.substring(0, bracketIdx) : entry;
          if (baseName) {
            const name = baseName
              .replace(/-/g, " ")
              .replace(/\b\w/g, (c) => c.toUpperCase());
            models.push({
              id: `${agent}:${entry}`,
              name: `${name} (${agent})`,
              provider: `acpx-${agent}`,
            });
          }
        }
      }

      resolve(models);
    });

    proc.on("error", (err) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeout);
        reject(err);
      }
    });
  });
}
