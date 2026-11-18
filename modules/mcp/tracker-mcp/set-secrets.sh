#!/bin/bash
# Puts the two GitHub secrets into the tracker-mcp Worker. Run from anywhere.
# Each prompt is hidden: paste the value and press Enter.
set -e
cd "$(dirname "$0")"
echo "1/2  Paste the OAuth app's CLIENT SECRET (GitHub → Developer settings → OAuth Apps → tracker-mcp → Generate a new client secret)"
npx wrangler secret put GITHUB_CLIENT_SECRET --name tracker-mcp
echo
echo "2/2  Paste the REPO TOKEN (GitHub → Developer settings → Fine-grained tokens → tracker-mcp)"
npx wrangler secret put GITHUB_TRACKER_TOKEN --name tracker-mcp
echo
echo "Done. Tell Claude 'secrets are in' so it can check the server."
