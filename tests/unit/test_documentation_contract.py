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


def test_documented_adoption_commands_match_cli_scope() -> None:
    design = (REPO_ROOT / "docs/superpowers/specs/2026-07-09-orchestration-v1-design.md").read_text(
        encoding="utf-8"
    )
    assert "untaped-orchestration check --local" in design
    assert "untaped-orchestration fmt --check --local" in design
    assert "untaped-orchestration render --check" in design
    assert "render --check --local" not in design


def test_changelog_records_010_release_date() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.1.0 (2026-07-15)" in changelog
    assert "0.1.0 (Unreleased)" not in changelog


def test_reviewed_implementation_contracts_use_durable_release_wording() -> None:
    design = (REPO_ROOT / "docs/superpowers/specs/2026-07-09-orchestration-v1-design.md").read_text(
        encoding="utf-8"
    )
    assert "Status: implemented" in design
    assert "Status: implemented; unreleased" not in design
    assert "Status: proposed, docs-only" not in design
    assert "does not authorize implementation" not in design
    assert "No implementation code belongs in the planning PR." not in design

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    cli = (REPO_ROOT / "docs/cli.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    normalized_cli = " ".join(cli.split())
    assert "Package-index availability is the source of truth" in normalized_readme
    assert (
        "Release availability is determined by package indexes and GitHub releases"
        in normalized_cli
    )
    for content in (readme, cli):
        assert "0.1.0 is implemented but unreleased" not in content
        assert "Version 0.1.0 remains unreleased" not in content

    required_by_path = {
        "README.md": (
            "UNTAPED_ISOLATED_WHEEL_TEST=1",
            "exactly one",
            "outside `dist/`",
        ),
        "AGENTS.md": (
            "dependency-resolving",
            "isolated install",
            "exactly one",
            "outside `dist/`",
        ),
        "CHANGELOG.md": ("archive metadata", "dependency-resolving", "isolated"),
        "SECURITY.md": ("generic ORC002", "failure receipt"),
        "docs/cli.md": (
            "Reads recurse by default",
            "`--local` is true-local",
            "failure receipt",
        ),
        "docs/file-format.md": (
            "64 KiB",
            "1 MiB",
            "component-wise no-follow",
            "cooperative writers",
        ),
        "docs/recovery.md": (
            "acknowledged changed paths",
            "generic ORC002",
            "recursive participant locks",
        ),
        "src/untaped_orchestration/skills/untaped-orchestration/SKILL.md": (
            "acknowledged changed paths",
            "64 KiB",
            "1 MiB",
        ),
        "docs/superpowers/plans/2026-07-09-orchestration-v1-implementation.md": (
            "UNTAPED_ISOLATED_WHEEL_TEST=1",
            "exactly one explicit isolated-install skip",
            "outside `dist/`",
        ),
    }
    for relative, terms in required_by_path.items():
        content = " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())
        for term in terms:
            assert term in content, f"{relative} is missing {term!r}"
