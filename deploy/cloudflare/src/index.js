import { Container } from "@cloudflare/containers";

export class LuxembourgMcp extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  // The image defaults to the stdio transport (MCP container convention);
  // the hosted deployment runs the HTTP transport instead.
  entrypoint = ["luxembourg-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"];
  enableInternet = true; // the server fetches Luxembourg's public upstream APIs
  envVars = {
    LUXEMBOURG_MCP_ALLOWED_ORIGINS: "*",
    LUXEMBOURG_MCP_CLIENT_IP_HEADER: "CF-Connecting-IP",
    LUXEMBOURG_MCP_RATE_LIMIT: "60",
  };
}

export default {
  async fetch(request, env) {
    // One instance per release: the name change on deploy routes traffic to a
    // fresh Durable Object (and therefore a fresh container on the new image),
    // because an existing DO keeps its originally provisioned container across
    // rolling deploys. Within a release it stays a singleton so the in-process
    // GTFS/STATEC caches are shared across all clients.
    const container = env.LUXEMBOURG_MCP.getByName(`main-${env.RELEASE}`);
    await container.startAndWaitForPorts();
    return container.fetch(request);
  },
};
