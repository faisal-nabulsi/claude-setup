#!/bin/sh
# Launch from the repo folder so .env and logs/writes.jsonl resolve here,
# whatever directory the Claude session started in.
cd "$(dirname "$0")" && exec node dist/index.js
