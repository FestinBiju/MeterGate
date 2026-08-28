import { fileURLToPath } from "node:url";

import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";

import {
  parseOrbitalIntent,
  ReferenceBuyerAgent,
  type PurchaseContext,
  type ToolCaller,
} from "./reference-buyer.js";

class SdkToolCaller implements ToolCaller {
  constructor(private readonly client: Client) {}

  async call(name: string, input: Record<string, unknown>): Promise<Record<string, unknown>> {
    const result = await this.client.callTool({ name, arguments: input });
    if (result.isError) {
      const failure = result.structuredContent;
      const code = isRecord(failure) && typeof failure.code === "string" ? failure.code : "MCP_TOOL_FAILED";
      throw new Error(code);
    }
    if (!isRecord(result.structuredContent)) {
      throw new Error("MCP tool returned no structured content");
    }
    return result.structuredContent;
  }
}

async function main(): Promise<void> {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL("./index.js", import.meta.url))],
    env: childEnvironment(),
  });
  const client = new Client({ name: "metergate-reference-buyer", version: "0.1.0" });
  await client.connect(transport);
  try {
    const agent = new ReferenceBuyerAgent(new SdkToolCaller(client));
    const resumeIndex = process.argv.indexOf("--resume");
    const result =
      resumeIndex >= 0
        ? await agent.resume(JSON.parse(process.argv[resumeIndex + 1] ?? "") as PurchaseContext)
        : await agent.begin(parseOrbitalIntent(process.argv.slice(2).join(" ")));
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } finally {
    await client.close();
  }
}

function childEnvironment(): Record<string, string> {
  const result: Record<string, string> = {};
  for (const name of ["PATH", "PATHEXT", "SystemRoot", "TEMP", "TMP"] as const) {
    const value = process.env[name];
    if (value) result[name] = value;
  }
  for (const name of ["METERGATE_API_URL", "METERGATE_MCP_TOKEN"] as const) {
    const value = process.env[name];
    if (value) result[name] = value;
  }
  return result;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

main().catch((error: unknown) => {
  process.stderr.write(
    `${error instanceof Error ? error.message : "Reference buyer failed safely"}\n`,
  );
  process.exitCode = 1;
});
