# Canonical file format

The canonical store lives at `.untaped/orchestration/`. `store.toml` declares
the store identity, visibility, task capability, timezone, curation intervals,
and brief bounds. `registry.toml` explicitly registers child stores by expected
immutable store ID and relative POSIX path. There is no ambient discovery.

Tasks live under `tasks/`; closed tasks live under `archive/tasks/`; decisions
live under `decisions/`. `views/` contains deterministic human projections.
`.lock`, sibling atomic temporary files, and editor artifacts are runtime state,
not canonical data.

## TOML metadata and opaque Markdown

Every item is UTF-8 without a BOM, begins at byte zero with `+++\n`, contains
strict TOML 1.0 front matter, and closes metadata at the next line containing
exactly `+++`. Front matter is bounded to 64 KiB and the Markdown body to 1 MiB.
Pydantic rejects unknown fields, and TOML duplicate keys are syntax errors.

The body is opaque Markdown. The tool does not parse headings, normalize prose,
or build a Markdown AST. It needs structured metadata for queries and lifecycle
rules, while Git and humans own the prose. Formatting rewrites only validated
metadata and preserves every accepted body byte, newline style, and final-LF
choice.

Metadata serialization has fixed key order, canonical strings, sorted unique
tags, sorted links, and sorted evidence. The serializer reparses and validates
its own output before replacement. A hand-edited item becomes canonical only
when both `check` and `fmt --check` pass.

## Identity and revisions

Store, task, and decision IDs are caller-stable UUIDv7 values prefixed `sto_`,
`tsk_`, or `dec_`. Filenames combine the immutable ID with a cosmetic creation
slug; later title changes never rename the file. Retry a create with the same
ID and identical inputs.

Item revisions are SHA-256 of exact item bytes. The store revision hashes every
canonical local path and file hash except views, locks, and temporaries. The
registry revision hashes exact `registry.toml` bytes. Mutations accept the
specific revision guards documented by `--help`; agents always pass them.

## Privacy, capabilities, and views

In v1 public stores are decision-only. They cannot contain active or archived
tasks, and there is no public-task exception. Unfinished tasks belong in the
private hub. `check` rejects a hand edit that makes a populated task store
public or decision-only.

Generated views are local projections for humans. Agents never read or edit
them. Bodies, recursive federation content, private child data, and wall-clock
timestamps never appear in a view. Use `render --check` to detect drift and
`render --write` to regenerate applicable views.

See the [authoritative design](superpowers/specs/2026-07-09-orchestration-v1-design.md)
for every field, lifecycle transition, relation, and canonical byte rule.
