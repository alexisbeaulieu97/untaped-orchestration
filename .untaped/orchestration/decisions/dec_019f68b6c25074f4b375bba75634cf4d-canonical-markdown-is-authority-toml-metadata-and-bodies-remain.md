+++
schema = "untaped.orchestration.decision/v1"
id = "dec_019f68b6c25074f4b375bba75634cf4d"
kind = "decision"
title = "Canonical Markdown is authority; TOML metadata and bodies remain opaque"
created_at = "2026-07-16T00:23:30.000Z"
tags = []

[[evidence]]
relation = "tracked-by"
reference = "git:01318c5a6ecf58a8afb897d4f34cc5b350a5c6ae:docs/superpowers/specs/2026-07-09-orchestration-v1-design.md#sha256:52d973e40559b2607c04031afc6ac84bc8a341bf599d653abf27501f99db1320"
+++
## 2. Architecture

Canonical Markdown files are the only authority. The CLI parses typed TOML
front matter and treats every body byte after the delimiter as opaque. Reads
build bounded in-memory header models; body-heavy operations load or stream
only the bodies they need. Generated views are committed derived artifacts and
never become input to the tool.

Implementation dependency direction is:

```text
domain
  ↑
application
  ↑
infrastructure
  ↑
CLI/composition
```

The domain and application layers do not import filesystem, `filelock`, CLI,
or infrastructure modules. Application ports are defined inward before their
adapters: `Clock`, `IdGenerator`, `StoreReader`, `StoreWriter`, `LockManager`,
and `ViewRenderer`. There is no `Vcs`, transaction-journal, or provider port.

