import type { Plugin, Skill } from "@opencode/plugin";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const skills = join(root, "skills");
const server = { type: "local", command: ["uvx", "--from", root, "multimodal-mcp"] } as const;

// SKILL.md frontmatter holds single-line plain `name` and `description` values.
function readSkill() {
  const path = join(skills, "multimodal", "SKILL.md");
  const [, frontmatter, content] = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(readFileSync(path, "utf8"))!;
  const field = (key: string) => new RegExp(`^${key}:[ \\t]*(.+)$`, "m").exec(frontmatter)![1].trim();
  return { id: "multimodal", name: field("name"), description: field("description"), path, content } as Skill.Info;
}

// OpenCode 2.x calls `setup`; OpenCode 1.x calls `server` and applies its `config` hook before starting MCP servers.
export default {
  id: "multimodal",
  async setup(ctx) {
    await ctx.skill.transform((editor) => editor.add(readSkill()));
    await ctx.mcp.transform((editor) => {
      if (!editor.get("multimodal")) editor.set("multimodal", server);
    });
  },
  server: async () => ({
    config: async (config: { skills?: { paths?: string[] }; mcp?: Record<string, unknown> }) => {
      config.skills = { ...config.skills, paths: [...(config.skills?.paths ?? []), skills] };
      config.mcp = { multimodal: server, ...config.mcp };
    },
  }),
} satisfies Plugin.Plugin & { server: unknown };
