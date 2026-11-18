/**
 * Who may use this server.
 *
 * The Cloudflare template lets ANY GitHub user who finishes sign-in use the
 * tools. This server is private, so access is checked against an allowlist of
 * GitHub logins at three points:
 *
 *   1. OAuth callback (github-handler.ts): a login not on the list never gets
 *      an MCP token. This is the front door.
 *   2. API gate (gateApiHandler below): every /mcp request is checked against
 *      the token's login before it reaches the MCP Durable Object.
 *   3. Every tool handler (tools.ts): checked again at call time, right before
 *      the tool touches GitHub.
 *
 * Layers 2 and 3 read the allowlist on every request, so editing
 * ALLOWED_GITHUB_LOGINS (to "" for an instant lockout) also cuts off tokens
 * that were issued earlier.
 */

/** Used only when the ALLOWED_GITHUB_LOGINS variable is missing entirely. */
export const DEFAULT_ALLOWED_LOGINS = "YOUR-GITHUB-LOGIN";

/**
 * Parses "a, b c" into a set of lowercase logins. GitHub logins are
 * case-insensitive, so comparison is too. An empty string means nobody.
 */
export function parseAllowlist(raw: string | undefined): Set<string> {
	const value = raw === undefined ? DEFAULT_ALLOWED_LOGINS : String(raw);
	return new Set(
		value
			.split(/[\s,]+/)
			.map((s) => s.trim().toLowerCase())
			.filter(Boolean),
	);
}

export function isAllowedLogin(login: unknown, rawAllowlist: string | undefined): boolean {
	if (typeof login !== "string" || login.trim() === "") return false;
	return parseAllowlist(rawAllowlist).has(login.trim().toLowerCase());
}

export const ACCESS_DENIED_MESSAGE =
	"Access denied: this tracker is private and your GitHub account is not on its allowlist.";

type FetchHandler<E> = {
	fetch: (request: Request, env: E, ctx: ExecutionContext) => Response | Promise<Response>;
};

/**
 * Layer 2. Wraps the MCP API handler. The OAuth provider has already checked
 * the bearer token and put the grant's props on ctx.props; this refuses the
 * request unless props.login is on the allowlist right now.
 */
export function gateApiHandler<E extends { ALLOWED_GITHUB_LOGINS?: string }>(
	inner: FetchHandler<E>,
): FetchHandler<E> {
	return {
		fetch(request, env, ctx) {
			const login = (ctx as unknown as { props?: { login?: unknown } }).props?.login;
			if (!isAllowedLogin(login, env.ALLOWED_GITHUB_LOGINS)) {
				return new Response(ACCESS_DENIED_MESSAGE, {
					headers: { "Content-Type": "text/plain; charset=utf-8" },
					status: 403,
				});
			}
			return inner.fetch(request, env, ctx);
		},
	};
}
