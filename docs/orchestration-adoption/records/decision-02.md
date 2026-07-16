### 4.2 Front-matter grammar

Every item:

- Is UTF-8 without a byte-order mark.
- Starts at byte zero with `+++\n`.
- Contains TOML 1.0 metadata.
- Ends metadata at the next line containing exactly `+++`.
- Has at most 64 KiB of front matter and 1 MiB of Markdown body.
- Uses LF in rewritten front matter.
- Preserves every body byte after the closing delimiter, including newline
  style and final-newline presence.

`tomllib` parses the metadata and supplies syntax locations when available.
Pydantic models use `extra="forbid"`. Duplicate TOML keys are syntax errors.
No Markdown content is parsed or normalized.

Canonical metadata serialization uses fixed schema key order, canonical TOML
basic strings, sorted unique tags, links sorted by relation/store/ID, and
evidence sorted by relation/reference. Metadata comments are noncanonical and
`fmt --write` removes them. `tomli-w` output is reparsed and revalidated before
replacement.

