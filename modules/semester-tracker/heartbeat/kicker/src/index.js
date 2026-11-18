// Two jobs in one Worker:
//  1. scheduled(): triggers the heartbeat GitHub workflow via workflow_dispatch on a
//     reliable Cloudflare cron. Needs secret GH_TOKEN: a fine-grained PAT scoped to the
//     heartbeat repo with "Actions: Read and write".
//  2. fetch(): GET /todo phone glance (see GLANCE.md and src/glance.js). Needs secrets
//     GLANCE_KEY (bearer key the iOS Shortcut sends) and GITHUB_READ_TOKEN (fine-grained
//     PAT, this repo only, "Contents: Read-only").
import { handleRequest } from "./glance.js";

// Lives as long as this isolate; holds the last GitHub read for ~60 s.
const glanceCache = {};

export default {
  async fetch(request, env) {
    return handleRequest(request, env, { cache: glanceCache });
  },

  async scheduled(event, env, ctx) {
    const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "heartbeat-kicker",
      },
      body: JSON.stringify({ ref: env.REF }),
    });
    if (res.status !== 204) {
      // Throwing marks the cron invocation as failed in the dashboard/logs.
      throw new Error(`dispatch failed: ${res.status} ${await res.text()}`);
    }
    console.log(`dispatched ${env.WORKFLOW} at ${new Date(event.scheduledTime).toISOString()}`);
  },
};
