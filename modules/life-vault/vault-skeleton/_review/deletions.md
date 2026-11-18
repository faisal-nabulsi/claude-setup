# Deletions and suppressions

This file is a standing instruction, not just a log. `lv-forget` reads it and removes every
matching entry, every time it runs, so something you asked the vault to forget can't sneak
back in from a cached extraction job.

- `pattern: <regex>` lines are case-insensitive regexes matched against each entry's title,
  body, subject and quote.
- `keep: <subject>` lines name a subject that survives a pattern match (for example the
  record that something was false).

Add a short dated note above each pattern saying what it suppresses and why.
Claude must check this file before filing anything.

<!-- Example (fake):
## 2026-01-01 — invented classmate
pattern: \bcasey example\b
keep: casey-example-was-invented
-->
