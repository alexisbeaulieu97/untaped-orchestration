# Diagnostics and recovery

Start with `untaped-orchestration check --format json` and `git diff`. Diagnostics
are stable, ordered records with a code, severity, path, field, message, and
hint:

| Code | Meaning |
|---|---|
| `ORC001` | TOML or front-matter syntax |
| `ORC002` | Schema, field, or type violation |
| `ORC003` | ID, filename, path, or store identity mismatch |
| `ORC004` | Relation, cardinality, cycle, or graph violation |
| `ORC005` | Invalid or incomplete registry/federation |
| `ORC006` | Lifecycle, curation, close, or retirement invariant |
| `ORC007` | Revision or lock conflict |
| `ORC008` | Generated view missing or stale |
| `ORC009` | Visibility or capability policy violation |

Typed expected failures own those public fields and preserve their exact exit
mapping. Unexpected exceptions are redacted to a generic ORC002; their raw
message is not copied into JSON/table output. Canonical writer failures can
also carry a bounded receipt. Before any writer acknowledgement it is
`applied=false`, `canonical_applied=false`, and has no changed paths. After one
or more writer calls return, it reports the exact acknowledged changed paths,
`applied=true`, and `canonical_applied=true`. In both cases
`views_current=false`; typed failures keep their exact diagnostic and exit.

After a valid hand edit, run `check`, then `fmt --check`. `fmt --write` can
canonicalize valid TOML metadata under revision guards; it never invents a
missing semantic value, renames an item, or changes the opaque body.

## Concurrency and durability

Reads and writes acquire store-wide locks in normalized real-path order.
Existing-item, store, and registry mutations use current revision guards;
`--force-current` is human-only and never bypasses identity, lifecycle, graph,
privacy, lock, or filesystem checks.

Each file uses sibling temporary creation, flush/fsync, atomic replacement, and
parent-directory fsync. Multi-file operations validate the intended result
before their first write and permit only bounded, explicitly detectable fault
states. There is no write-ahead log, journal, hidden operation ID, Git adapter,
VCS adapter, or provider adapter. The canonical files, guarded retry protocol,
and targeted Git recovery are the recovery layer.

Accepted phases include a matching partial init scaffold, a fully durable
caller-stable create, a same-order rank rebalance before its primary mutation,
matched active/archive close copies, successor-before-pin decision phases,
exact manifest subsets, and stale derived views. `check` reports recoverable
states; it never accepts divergent content merely because it parses.

## Recovery procedure

1. Preserve unrelated work. Run `check --format json` and inspect `git diff`.
2. Read only the reported files and reread their current revisions.
3. If the files exactly match a documented accepted phase, retry with the same
   caller-stable ID and guarded intent, or use the targeted repair command.
4. Use `repair duplicate ... --apply` only for a semantically matched
   active/archive pair. Re-run the exact external import manifest to resume an
   import subset.
5. If state is divergent, use Git to restore only affected paths, or repair one
   file explicitly. Never default to a broad store restore.
6. Finish with `check`, `fmt --check`, and `render --check`.

## Broken front matter and byte-mode recovery

`show ID --raw` locates a safe filename prefix even when TOML is broken;
`inspect PATH --raw` targets a regular nonsymlink file when its ID or filename
cannot be trusted. With raw output, stdout is the exact file bytes and stderr is
one compact metadata JSON line. JSON mode returns padded base64 in the normal
envelope. Table and Pipe are rejected for recovery. On byte-mode failure,
stdout is zero bytes.

`repair frontmatter` validates replacement TOML and preserves a provable body
boundary. If delimiters or UTF-8 corruption make that boundary unknowable, an
explicit bounded `--body-file` is required; the tool never guesses.

Repair captures external inputs once, then acquires recursive participant locks,
rereads the exact raw revision, projects and validates the repaired federation
before writing, and validates the durable reread before finalizing views. A
canonical-success/view-failure receipt is recovered with `check` followed by
`render --write`; a canonical write failure is recovered only from its
acknowledged-path receipt plus the actual files and `git diff`.

External manifests, replacement front matter, and body files are read once as
bounded byte snapshots. Every path component must remain a nonsymlink, and the
opened object must be a regular file. On platforms without descriptor-relative
no-follow support, the reader uses cooperative pre-open and post-read identity
checks and rejects any substitution it detects; writers must not replace path
components while the read is in progress.
