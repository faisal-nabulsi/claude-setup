---
name: lock-in
description: Use when a fix has already failed once or more, when the user says work is sloppy / "you keep missing it" / "that's not it", when about to ship fix attempt #2+ for the same symptom, or when debugging has drifted into stacking theories instead of removing a regression. Enforces revert-before-add, all-call-paths verification, and evidence-before-claims.
---

# Lock In

For when the work has gone sloppy: repeated failed fixes, growing patches, a
frustrated user. The goal is to stop the bleeding and get rigorous.

## The three failures this prevents

Every one of these is a real, expensive mistake:

1. **Stacking theories instead of reverting.** Six "fixes" shipped for one bug,
   none of them removing what actually broke it. The changes were new
   inventions layered on a regression that was never undone.
2. **Fixing one call path and declaring victory.** A filter added to `addLog()`
   while the data actually arrived through `appendLogs()`. Told the user it was
   fixed. It wasn't. They had to report the same problem again.
3. **Theorising instead of instrumenting.** Days of inferring cause from
   symptoms when one log line would have settled it. The instrumentation should
   have come first, not fifth.

## Gate: before writing ANY fix

Stop and answer these. In writing, to the user, before touching code.

```
1. Attempt number for this symptom?           → if ≥2, the Escalation Ladder below is MANDATORY
2. Is this a REGRESSION (worked before)?      → if yes, you must find and revert the change; a new
                                                 guard/flag/handler is NOT a fix
3. What EVIDENCE do I have, vs what am I inferring?
                                              → if the answer is "inferring", go get evidence first
4. What would prove me WRONG?                 → if nothing could, it's not a hypothesis
```

## The Escalation Ladder (attempt ≥2)

Do not skip rungs. Each rung is cheaper than the one below it.

**Rung 1 — Revert, don't add.**
`git log -S"<symbol>"` and `git diff <known-good> HEAD -- <file>` the affected path.
Enumerate every guard, early return, flag, and timeout added since it worked.
Delete them. Restoring old behaviour beats inventing new behaviour.
A fix that *adds* code to something that used to work is suspect by default.

**Rung 2 — Instrument, don't theorise.**
If the evidence can't distinguish two hypotheses, you cannot pick between them.
Add logging that names the exact branch taken and the real error text, ship that,
and wait. Never `free`/discard an error object without reading its message first —
that discards the answer.

**Rung 3 — Verify the plumbing reaches you.**
Before trusting any diagnostic: does the signal actually survive to where you read
it? Check buffer caps, filters, truncation, and every ingestion path. Diagnostics
that get evicted or bypassed are worse than none — they look like evidence.

**Rung 4 — Question the architecture.**
Three-plus failures means the layer is wrong, not the details. Say so plainly and
discuss before attempting a fourth patch at the same layer.

## All-call-paths rule

Before claiming any change is complete:

```bash
grep -rn "<function>\|<property>" <src> | grep -v test
```

Enumerate **every** caller / ingestion point / writer. A change that covers one of
three paths is not done — and saying it is done burns the user's trust and their
time. If a batch path and a single-item path both exist, both need the change.

## Claims discipline

| Never say | Say instead |
|---|---|
| "Fixed." | "Compiles and installed — untested; here's what to check." |
| "This is the root cause." | "This explains E1–E5; it does not explain E6." |
| "Should work now." | "If it still fails, send X — that distinguishes A from B." |

Compiling is not working. Installing is not working. **Only the user's
confirmation is working.** State what was verified and what wasn't, every time.

## Preserve before you revert

Before a large revert or strip-down, save the current state and say where:

```bash
git tag -f <name>-shipped HEAD && git branch -f backup/<name> HEAD
```

Never let "go back to what worked" cost the user their working build.

## Owning it

When the user says the work is sloppy, they are almost always right and specific.

- Acknowledge the concrete mistake in one sentence. No throat-clearing, no
  performative self-flagellation.
- Fix that mistake before continuing.
- Say what you're changing about your approach — then actually change it.
- Do not re-litigate. Do not explain at length why it happened.

## Red flags — stop immediately

- "Let me also add a guard for…" → you're stacking. Revert instead.
- "It's probably…" → you're inferring. Instrument instead.
- "That should do it." → you don't know. Say what would prove it.
- Patch is bigger than the change that broke it → wrong direction.
- Can't name the commit that introduced a regression → you haven't looked hard enough.
