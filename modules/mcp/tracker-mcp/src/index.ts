import OAuthProvider from "@cloudflare/workers-oauth-provider";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { gateApiHandler } from "./access";
import { GitHubHandler } from "./github-handler";
import { registerTools } from "./tools";
import type { Props } from "./utils";

/**
 * One Durable Object per MCP session. this.props comes from the OAuth grant
 * (set in github-handler.ts /callback) and carries the GitHub login.
 */
export class TrackerMCP extends McpAgent<Env, Record<string, never>, Props> {
	server = new McpServer({
		name: "the user's tracker",
		version: "0.1.0",
	});

	async init() {
		// The context is read at call time, so each tool call re-checks the
		// current login against the current ALLOWED_GITHUB_LOGINS.
		registerTools(this.server, () => ({ env: this.env, login: this.props?.login }));
	}
}

export default new OAuthProvider({
	// Layer 2 of the access check wraps the MCP endpoint (see access.ts).
	apiHandler: gateApiHandler<Env>(TrackerMCP.serve("/mcp")),
	apiRoute: "/mcp",
	authorizeEndpoint: "/authorize",
	// Claude's connector default ("Claude's published identity") is a Client ID
	// Metadata Document; dynamic registration (/register) stays available as
	// the fallback ("Register automatically"). Needs the
	// global_fetch_strictly_public compatibility flag (wrangler.jsonc).
	clientIdMetadataDocumentEnabled: true,
	clientRegistrationEndpoint: "/register",
	// biome-ignore lint/suspicious/noExplicitAny: Hono app typing, as in the template.
	defaultHandler: GitHubHandler as any,
	tokenEndpoint: "/token",
});
