# Life Vault

Root: `~/life/`. Append-only markdown. Facts are never overwritten.

## How to find something

1. Resolve the name: `grep -i "<name>" ~/life/_index/entities.tsv` -> slug + path
2. Narrow by tag: read `~/life/_index/tags/<facet>-<value>.txt` -> list of `path#date`
3. Intersect two tags: `comm -12 <(sort a.txt) <(sort b.txt)`
4. Read only the files that survived. Never grep the whole vault.

## Rules

- Never edit the prose of an existing entry. Append a new one.
- `conf/derived` facts never supersede `conf/stated` facts.
- Entries tagged `sens/private` are never included in outbound content without asking.
- Unresolved contradictions live in `_review/conflicts.md`.
- Check `_review/deletions.md` before filing anything.

## Structure

`people/ orgs/ projects/ topics/ threads/ places/` each hold `<slug>/index.md` + `<slug>/log.md`.
`timeline/YYYY/YYYY-MM.md` holds dated events.
`_index/` is derived: rebuild with `tools/bin/lv reindex`, never edit by hand.
