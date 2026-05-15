import { Type } from "typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";

interface ToolsConfig {
  serverUrl: string;
  authToken: string;
  jobId: string;
  jiraUrl?: string;
  jiraEmail?: string;
  jiraToken?: string;
  githubToken?: string;
  githubRepo?: string;
}

async function httpGet(url: string, headers: Record<string, string> = {}): Promise<any> {
  const response = await fetch(url, { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

function authHeaders(token: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function createTools(config: ToolsConfig) {
  const tools: any[] = [];

  // Always available: job data tool
  tools.push(
    defineTool({
      name: "rootcoz_job",
      label: "Job Data",
      description:
        "Query analyzed job data. Commands: 'summary' (job overview), 'failures' (list all failures), " +
        "'failure' (details for a specific failure by UUID), 'comments' (job comments), " +
        "'history' (failure history for a test name).",
      parameters: Type.Object({
        command: Type.Union([
          Type.Literal("summary"),
          Type.Literal("failures"),
          Type.Literal("failure"),
          Type.Literal("comments"),
          Type.Literal("history"),
        ], { description: "The command to run" }),
        argument: Type.Optional(Type.String({ description: "UUID for 'failure' command, test_name for 'history' command" })),
      }),
      execute: async (_toolCallId, params) => {
        const headers = authHeaders(config.authToken);
        const baseUrl = config.serverUrl;
        const jobId = config.jobId;

        let data: any;
        switch (params.command) {
          case "summary":
          case "failures":
          case "failure": {
            const result = await httpGet(`${baseUrl}/results/${jobId}`, headers);
            data = result.result || result;
            if (params.command === "failure" && params.argument) {
              // Find specific failure by UUID
              const allFailures = collectFailures(data);
              const found = allFailures.find((f: any) => f.id === params.argument);
              data = found || { error: `Failure ${params.argument} not found` };
            }
            break;
          }
          case "comments":
            data = await httpGet(`${baseUrl}/results/${jobId}/comments`, headers);
            break;
          case "history":
            if (!params.argument) {
              data = { error: "test_name argument is required for history command" };
            } else {
              data = await httpGet(`${baseUrl}/history/test/${encodeURIComponent(params.argument)}`, headers);
            }
            break;
        }

        return {
          content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }],
          details: {},
        };
      },
    })
  );

  // Jira tool — only if configured
  if (config.jiraUrl && config.jiraToken) {
    tools.push(
      defineTool({
        name: "rootcoz_jira",
        label: "Jira",
        description:
          "Search and query Jira issues. Commands: 'search' (search by text query), " +
          "'issue' (get details for a specific issue key like CNV-12345), " +
          "'related' (find Jira tickets related to a failure UUID from the analysis).",
        parameters: Type.Object({
          command: Type.Union([
            Type.Literal("search"),
            Type.Literal("issue"),
            Type.Literal("related"),
          ], { description: "The command to run" }),
          argument: Type.String({ description: "Search query for 'search', issue key for 'issue', failure UUID for 'related'" }),
          project: Type.Optional(Type.String({ description: "Jira project key filter (e.g., CNV)" })),
          max_results: Type.Optional(Type.Number({ description: "Max results for search (default 10)" })),
        }),
        execute: async (_toolCallId, params) => {
          const jiraBase = config.jiraUrl!.replace(/\/$/, "");
          const isCloud = !!config.jiraEmail;
          const apiVersion = isCloud ? "3" : "2";
          const searchPath = isCloud ? `/rest/api/3/search/jql` : `/rest/api/2/search`;

          const jiraHeaders: Record<string, string> = { "Content-Type": "application/json" };
          if (isCloud && config.jiraEmail) {
            jiraHeaders["Authorization"] = "Basic " + Buffer.from(`${config.jiraEmail}:${config.jiraToken}`).toString("base64");
          } else {
            jiraHeaders["Authorization"] = `Bearer ${config.jiraToken}`;
          }

          let data: any;
          switch (params.command) {
            case "search": {
              const jql = params.project
                ? `project = "${params.project}" AND text ~ "${params.argument}" ORDER BY updated DESC`
                : `text ~ "${params.argument}" ORDER BY updated DESC`;
              const url = `${jiraBase}${searchPath}?jql=${encodeURIComponent(jql)}&maxResults=${params.max_results || 10}&fields=summary,status,assignee,priority,created`;
              const resp = await fetch(url, { headers: jiraHeaders });
              data = await resp.json();
              break;
            }
            case "issue": {
              const url = `${jiraBase}/rest/api/${apiVersion}/issue/${params.argument}`;
              const resp = await fetch(url, { headers: jiraHeaders });
              data = await resp.json();
              break;
            }
            case "related": {
              // Look up failure's jira_matches from the job result
              const headers = authHeaders(config.authToken);
              const result = await httpGet(`${config.serverUrl}/results/${config.jobId}`, headers);
              const jobData = result.result || result;
              const allFailures = collectFailures(jobData);
              const failure = allFailures.find((f: any) => f.id === params.argument);
              data = failure?.product_bug_report?.jira_matches || { message: "No Jira matches found for this failure" };
              break;
            }
          }

          return {
            content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }],
            details: {},
          };
        },
      })
    );
  }

  // GitHub tool — only if configured
  if (config.githubToken && config.githubRepo) {
    tools.push(
      defineTool({
        name: "rootcoz_github",
        label: "GitHub",
        description:
          "Search and query GitHub issues and PRs. Commands: 'search' (search issues/PRs by query), " +
          "'issue' (get details for a specific issue number), " +
          "'pr' (get details for a specific PR number).",
        parameters: Type.Object({
          command: Type.Union([
            Type.Literal("search"),
            Type.Literal("issue"),
            Type.Literal("pr"),
          ], { description: "The command to run" }),
          argument: Type.String({ description: "Search query for 'search', number for 'issue'/'pr'" }),
          state: Type.Optional(Type.String({ description: "Filter by state: open, closed, all (default: all)" })),
          max_results: Type.Optional(Type.Number({ description: "Max results for search (default 10)" })),
        }),
        execute: async (_toolCallId, params) => {
          const ghHeaders = {
            Authorization: `Bearer ${config.githubToken}`,
            Accept: "application/vnd.github.v3+json",
            "User-Agent": "rootcoz-sidecar",
          };

          let data: any;
          switch (params.command) {
            case "search": {
              let q = `${params.argument} repo:${config.githubRepo}`;
              if (params.state && params.state !== "all") {
                q += ` state:${params.state}`;
              }
              const url = `https://api.github.com/search/issues?q=${encodeURIComponent(q)}&per_page=${params.max_results || 10}`;
              const resp = await fetch(url, { headers: ghHeaders });
              data = await resp.json();
              break;
            }
            case "issue": {
              const url = `https://api.github.com/repos/${config.githubRepo}/issues/${params.argument}`;
              const resp = await fetch(url, { headers: ghHeaders });
              data = await resp.json();
              break;
            }
            case "pr": {
              const url = `https://api.github.com/repos/${config.githubRepo}/pulls/${params.argument}`;
              const resp = await fetch(url, { headers: ghHeaders });
              data = await resp.json();
              break;
            }
          }

          return {
            content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }],
            details: {},
          };
        },
      })
    );
  }

  return tools;
}

// Helper: recursively collect all failures from job data
function collectFailures(data: any): any[] {
  const failures: any[] = [...(data.failures || [])];
  for (const child of data.child_job_analyses || []) {
    failures.push(...(child.failures || []));
    if (child.child_job_analyses) {
      failures.push(...collectFailures(child));
    }
  }
  return failures;
}
