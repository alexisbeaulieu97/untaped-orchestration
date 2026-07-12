# Contributing

Use Python 3.14 and `uv`. Read `AGENTS.md` and the authoritative v1 design
before changing a public contract. Start with a failing focused test, implement
the smallest coherent change, and update the packaged skill and documentation
when commands, guards, output, recovery, or privacy behavior changes.

Before proposing a pull request, run:

```sh
uv --cache-dir .uv-cache run ruff check .
uv --cache-dir .uv-cache run ruff format --check .
uv --cache-dir .uv-cache run mypy
uv --cache-dir .uv-cache build --no-sources
uv --cache-dir .uv-cache run pytest
uv --cache-dir .uv-cache run pre-commit run --all-files --show-diff-on-failure
git diff --check
```

Do not weaken schema, coverage, or fault-state gates to make a change pass.
Keep commits focused and unsigned. A push, PR, merge, workflow dispatch,
release, publication, or self-adoption change needs its own explicit approval.
