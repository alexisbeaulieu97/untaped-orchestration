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

