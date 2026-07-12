from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_documentation_surface_and_links_are_complete() -> None:
    required = (
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/file-format.md",
        "docs/recovery.md",
        "docs/cli.md",
    )
    for relative in required:
        assert (REPO_ROOT / relative).is_file(), relative

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for target in (
        "docs/superpowers/specs/2026-07-09-orchestration-v1-design.md",
        "docs/file-format.md",
        "docs/recovery.md",
        "docs/cli.md",
        "https://github.com/alexisbeaulieu97/untaped/blob/main/docs/plugins.md",
    ):
        assert target in readme
    assert "uv tool install untaped-orchestration" in readme
    assert "git+https://github.com/alexisbeaulieu97/untaped-orchestration.git" in readme
    assert "published" in readme and "separate approval" in readme


def test_operator_docs_cover_store_output_recovery_privacy_and_rollout_contracts() -> None:
    corpus = "\n".join(
        (REPO_ROOT / path).read_text(encoding="utf-8")
        for path in ("README.md", "docs/file-format.md", "docs/recovery.md", "docs/cli.md")
    )
    required_terms = (
        "opaque Markdown",
        "Markdown AST",
        "ORC001",
        "ORC008",
        "atomic replacement",
        "no write-ahead log",
        "--force-current",
        "public stores are decision-only",
        "explicit federation",
        "orchestration.status",
        "byte-mode",
        "Market PR #6",
        "HTTPS `FETCH_HEAD`",
        "content cohort",
        "empty-store cohort",
        "hub last",
        "one separately reviewed adoption PR",
    )
    for term in required_terms:
        assert term in corpus


def test_changelog_keeps_010_unreleased() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "0.1.0" in changelog
    assert "Unreleased" in changelog
    assert "published" not in changelog.casefold()
