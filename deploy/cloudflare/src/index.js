import { Container } from "@cloudflare/containers";

export class LuxembourgMcp extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  enableInternet = true; // the server fetches Luxembourg's public upstream APIs
  envVars = {
    LUXEMBOURG_MCP_ALLOWED_ORIGINS: "*",
    LUXEMBOURG_MCP_CLIENT_IP_HEADER: "CF-Connecting-IP",
    LUXEMBOURG_MCP_RATE_LIMIT: "60",
  };
}

export default {
  async fetch(request, env) {
    // Singleton instance so the in-process GTFS/STATEC caches are shared
    // across all clients instead of rebuilt per container.
    const container = env.LUXEMBOURG_MCP.getByName("main");
    await container.startAndWaitForPorts();
    return container.fetch(request);
  },
};
