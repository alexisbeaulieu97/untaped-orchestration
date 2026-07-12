# untaped-orchestration

`untaped-orchestration` is a standalone Python 3.14 CLI for Git-native, typed
repository tasks and decisions. Canonical state is reviewable text under
`.untaped/orchestration/`; agents use bounded queries and guarded mutations
instead of loading or rewriting the store directly.

Status: **0.1.0 is implemented but unreleased**. The repository does not
contain a self-adoption store, and the commands below do not imply that a PyPI
publication has happened.

## Install

After the separately approved 0.1.0 release:

```sh
uv tool install untaped-orchestration
```

For source evaluation before release:

```sh
uv tool install 'git+https://github.com/alexisbeaulieu97/untaped-orchestration.git'
```

Run `untaped-orchestration --help` for the command tree. The safest agent entry
point in an existing store is:

```sh
untaped-orchestration brief --format json
```

Allocate a caller-stable ID before an initialization or create operation, and
reuse it on every retry:

```sh
STORE_ID="$(untaped-orchestration id new store --format raw)"
untaped-orchestration init . --store-id "$STORE_ID" --name Example --timezone UTC
```

## Documentation

- [CLI and output contracts](docs/cli.md)
- [Canonical file format](docs/file-format.md)
- [Diagnostics and recovery](docs/recovery.md)
- [Authoritative v1 design](docs/superpowers/specs/2026-07-09-orchestration-v1-design.md)
- [SDK plugin guide](https://github.com/alexisbeaulieu97/untaped/blob/main/docs/plugins.md)
- [Contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md)

## Privacy and federation

Public stores are decision-only in v1. Tasks remain in a private task-capable
store. The explicit federation registry is `registry.toml`; the tool never
discovers nearby repositories implicitly. Partial-tolerant reads report incompleteness,
while readiness, delivery, and structural mutations fail closed.

## Release and self-adoption gate

Do not create `.untaped/orchestration` in this repository until
`untaped-orchestration==0.1.0` is published, its fresh `uvx` smoke passes, and
Alexis gives separate approval for self-adoption. Adoption is a later,
separately reviewed PR using the exact released pin; it is not part of this
implementation branch.

Fleet rollout is staged: dry-run pilot, content cohort, empty-store cohort, and
the private hub last. Every repository receives one separately reviewed
adoption PR. See [CLI and output contracts](docs/cli.md#release-and-rollout-gates)
for the exact gates.
