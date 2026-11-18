/**
 * Constructs an authorization URL for an upstream service.
 *
 * Changed from the template: `scope` is optional. tracker-mcp asks GitHub for
 * no scopes at all, because it only needs to learn who signed in (the public
 * `login`), never to act on the user's GitHub account.
 */
export function getUpstreamAuthorizeUrl({
	upstream_url,
	client_id,
	scope,
	redirect_uri,
	state,
}: {
	upstream_url: string;
	client_id: string;
	scope?: string;
	redirect_uri: string;
	state?: string;
}) {
	const upstream = new URL(upstream_url);
	upstream.searchParams.set("client_id", client_id);
	upstream.searchParams.set("redirect_uri", redirect_uri);
	if (scope) upstream.searchParams.set("scope", scope);
	if (state) upstream.searchParams.set("state", state);
	upstream.searchParams.set("response_type", "code");
	return upstream.href;
}

/**
 * Fetches an authorization token from an upstream service.
 *
 * @returns {Promise<[string, null] | [null, Response]>} the access token, or an error response.
 */
export async function fetchUpstreamAuthToken({
	client_id,
	client_secret,
	code,
	redirect_uri,
	upstream_url,
}: {
	code: string | undefined;
	upstream_url: string;
	client_secret: string;
	redirect_uri: string;
	client_id: string;
}): Promise<[string, null] | [null, Response]> {
	if (!code) {
		return [null, new Response("Missing code", { status: 400 })];
	}

	const resp = await fetch(upstream_url, {
		body: new URLSearchParams({ client_id, client_secret, code, redirect_uri }).toString(),
		headers: {
			"Content-Type": "application/x-www-form-urlencoded",
		},
		method: "POST",
	});
	if (!resp.ok) {
		console.log(`GitHub token exchange failed: HTTP ${resp.status}`);
		return [null, new Response("Failed to fetch access token", { status: 500 })];
	}
	const body = await resp.formData();
	const accessToken = body.get("access_token") as string;
	if (!accessToken) {
		return [null, new Response("Missing access token", { status: 400 })];
	}
	return [accessToken, null];
}

/**
 * Fetches the signed-in GitHub user's public identity with their (scope-less)
 * user token. Replaces the template's Octokit call so the Worker needs no
 * GitHub SDK.
 */
export async function fetchGitHubUser(
	accessToken: string,
): Promise<{ login: string; name: string | null; id: number } | null> {
	const resp = await fetch("https://api.github.com/user", {
		headers: {
			Accept: "application/vnd.github+json",
			Authorization: `Bearer ${accessToken}`,
			"User-Agent": "tracker-mcp",
			"X-GitHub-Api-Version": "2022-11-28",
		},
	});
	if (!resp.ok) return null;
	const data = (await resp.json()) as { login?: unknown; name?: unknown; id?: unknown };
	if (typeof data.login !== "string" || data.login.length === 0) return null;
	return {
		id: typeof data.id === "number" ? data.id : -1,
		login: data.login,
		name: typeof data.name === "string" ? data.name : null,
	};
}

// Context from the auth process, encrypted & stored in the auth token
// and provided to the Durable Object as this.props.
// Changed from the template: the user's GitHub access token is NOT stored,
// because no tool acts as the user on GitHub (repo access uses a separate,
// repo-scoped token held as a Worker secret).
export type Props = {
	login: string;
	name: string | null;
};
