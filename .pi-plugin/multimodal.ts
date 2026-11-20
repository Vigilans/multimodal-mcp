import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

// A same-name server in mcp.json takes precedence over this registration.
export default function (pi: ExtensionAPI) {
  pi.registerMcpServer("multimodal", { command: "uvx", args: ["--from", root, "multimodal-mcp"] });
}
