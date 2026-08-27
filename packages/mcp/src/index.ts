import { serveStdio } from "@modelcontextprotocol/server/stdio";

import { clientFromEnvironment } from "./api-client.js";
import { createMeterGateServer } from "./server.js";

void serveStdio(() => createMeterGateServer(clientFromEnvironment()));
console.error("MeterGate MCP server listening on stdio");
