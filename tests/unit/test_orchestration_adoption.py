"""Repository contract for orchestration's public v1 self-adoption."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
STORE = ROOT / ".untaped/orchestration"
ADOPTION = ROOT / "docs/orchestration-adoption"
STORE_ID = "sto_019f68b6b1c4721b8cd64f9e1426c34d"
SOURCE_OID = "01318c5a6ecf58a8afb897d4f34cc5b350a5c6ae"
SOURCE_PATH = "docs/superpowers/specs/2026-07-09-orchestration-v1-design.md"
SOURCE_SHA = "52d973e40559b2607c04031afc6ac84bc8a341bf599d653abf27501f99db1320"
SOURCE_REF = f"git:{SOURCE_OID}:{SOURCE_PATH}#sha256:{SOURCE_SHA}"
DECISION_IDS = (
    "dec_019f68b6c25074f4b375bba75634cf4d",
    "dec_019f68b6c3677043928e09f4e89ecab6",
    "dec_019f68b6c48576e49388dec9e464b5b6",
    "dec_019f68b6c58f73bc92ac6dbfd1983fab",
    "dec_019f68b6c69772609adcfad1532c7626",
    "dec_019f68b6c79e7153b69b0137e8aaed5d",
)
RANGES = ("60-84", "284-306", "637-666", "848-882", "1032-1093", "1206-1226")
TERMINAL_LINES = (84, 306, 666, 882, 1093, 1226)
BYTE_COUNTS = (794, 942, 1694, 1964, 6326, 809)
BLOCK_HASHES = (
    "995136f375e960f9583deb5bbc750808d4c7fdbe5087bfe0d794e0fc90020de0",
    "d1eba38f23b779e0aa22644a08e53eede24adf843a47a11fd8c650025931ba16",
    "6408f6f12e6ce9230e3f1fecb9444a61dfd1a34eda7b9b43af0ee091876a1b0e",
    "42e0140fa25359cb3b02131d225ed4dd81351ee3a4234008c2dbc8fc407fce0c",
    "b44c3ee526609916bdffb1b1f05d148242bfbde7b66ebc34bd8346c6e1ebaee6",
    "d8c7dd119aa4711d976ba83c651d4d6eb79f62809c6108a7c39a11cddee73944",
)
FINAL_STORE_REVISION = "sha256:eaaf016bba996d9a62712439518759a4cc9b861dfc68f77d3e55fd2e6de212ea"


def load_toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def parse_item(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    assert raw.startswith(b"+++\n")
    _, frontmatter, body = raw.split(b"+++\n", 2)
    return tomllib.loads(frontmatter.decode()), body


def test_store_is_public_decision_only_childless_and_pinned() -> None:
    store = load_toml(STORE / "store.toml")
    assert store["schema"] == "untaped.orchestration.store/v1"
    assert store["id"] == STORE_ID
    assert store["name"] == "untaped-orchestration"
    assert store["visibility"] == "public"
    assert store["timezone"] == "UTC"
    assert store["capabilities"] == {"active_tasks": False}
    assert store["brief"]["pinned_decisions"] == list(DECISION_IDS)
    assert load_toml(STORE / "registry.toml") == {
        "schema": "untaped.orchestration.registry/v1",
        "store_id": STORE_ID,
    }
    assert not list((STORE / "tasks").glob("*.md")) if (STORE / "tasks").exists() else True


def test_brief_is_exactly_pinned_bounded_and_truthfully_truncated() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from untaped_orchestration.__main__ import main; main()",
            "brief",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    encoded = result.stdout.encode()
    envelope = json.loads(result.stdout)
    assert len(encoded) <= 32768
    assert envelope["complete"] is True
    assert envelope["truncated"] is True
    assert envelope["data"]["store_revision"] == FINAL_STORE_REVISION
    pinned = envelope["data"]["pinned_decisions"]
    assert [entry["item_id"] for entry in pinned] == list(DECISION_IDS)
    source_bodies = [
        (ADOPTION / f"records/decision-{index:02}.md").read_bytes() for index in range(1, 7)
    ]
    for index in (0, 1, 2, 3, 5):
        assert pinned[index]["body"].encode() == source_bodies[index]
    assert pinned[4]["body"].encode() == source_bodies[4][:4096]
    assert pinned[4]["body"].encode() != source_bodies[4]


def test_selected_decisions_and_records_preserve_exact_blocks() -> None:
    decision_paths = sorted((STORE / "decisions").glob("*.md"))
    assert len(decision_paths) == 6
    decisions = {
        frontmatter["id"]: (frontmatter, body)
        for frontmatter, body in map(parse_item, decision_paths)
    }
    assert tuple(decisions) == DECISION_IDS
    for index, (decision_id, size, digest) in enumerate(
        zip(DECISION_IDS, BYTE_COUNTS, BLOCK_HASHES, strict=True), start=1
    ):
        frontmatter, body = decisions[decision_id]
        assert frontmatter["schema"] == "untaped.orchestration.decision/v1"
        assert frontmatter["kind"] == "decision"
        assert frontmatter["created_at"] == "2026-07-16T00:23:30.000Z"
        assert frontmatter["evidence"] == [{"relation": "tracked-by", "reference": SOURCE_REF}]
        assert len(body) == size
        assert hashlib.sha256(body).hexdigest() == digest
        assert body.endswith(b"\n\n")
        record = load_toml(ADOPTION / f"records/decision-{index:02}.toml")
        assert record["id"] == decision_id
        assert record["evidence"] == frontmatter["evidence"]
        assert (ADOPTION / f"records/decision-{index:02}.md").read_bytes() == body


def test_selected_source_manifest_explicitly_does_not_cover_full_design() -> None:
    manifest = load_toml(ADOPTION / "decision-sources.toml")
    assert manifest["schema"] == "untaped.orchestration.decision-sources/v1"
    assert manifest["scope"] == "selected-tool-decisions"
    assert manifest["full_file_coverage"] is False
    assert manifest["source_repository"] == "untaped-orchestration"
    assert manifest["source_oid"] == SOURCE_OID
    assert manifest["source_path"] == SOURCE_PATH
    assert manifest["source_sha256"] == SOURCE_SHA
    assert manifest["source_bytes"] == 75697
    assert manifest["source_lines"] == 1477
    assert manifest["source_timestamp"] == "2026-07-16T00:23:30.000Z"
    assert "remain authoritative" in manifest["nonselected_lines"]
    assert "neither migrated nor dispositioned" in manifest["nonselected_lines"]
    selections = manifest["selections"]
    assert [entry["line_range"] for entry in selections] == list(RANGES)
    assert [entry["terminal_line"] for entry in selections] == list(TERMINAL_LINES)
    assert [entry["source_bytes"] for entry in selections] == list(BYTE_COUNTS)
    assert [entry["block_sha256"] for entry in selections] == list(BLOCK_HASHES)
    assert [entry["destination_id"] for entry in selections] == list(DECISION_IDS)
    assert all(entry["body_includes_terminal_line_lf"] is True for entry in selections)
    assert manifest["review_evidence"] == "review.md"
    assert {entry["review_status"] for entry in selections} == {"accepted"}
    review = (ADOPTION / "review.md").read_text(encoding="utf-8")
    assert "## Verdict: ACCEPT" in review
    assert "Independent reviewer: Codex review subagent `market_permission_auditor`" in review
    assert (
        "01318c5a6ecf58a8afb897d4f34cc5b350a5c6ae..dbd3f856eab8af1c91ce8393b923f89d9d1da615"
    ) in review
    assert SOURCE_SHA in review


def test_import_pointer_agent_rules_ignores_and_workflow() -> None:
    manifest = load_toml(ADOPTION / "import.toml")
    assert manifest["schema"] == "untaped.orchestration.import/v1"
    assert manifest["target_store_id"] == STORE_ID
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["expected_store_revision"])
    assert manifest["require_empty_items"] is True
    assert [
        load_toml(ADOPTION / row["frontmatter_file"])["id"] for row in manifest["records"]
    ] == list(DECISION_IDS)
    assert [row["source_ref"] for row in manifest["records"]] == [SOURCE_REF] * 6

    pointer = (ROOT / "docs/decisions.md").read_text(encoding="utf-8")
    assert "../.untaped/orchestration/views/decisions.md" in pointer
    assert "untaped-orchestration brief --format json" in pointer
    assert "six" in pointer and "normative" in pointer and "generated" in pointer
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in (
        "public decision-only",
        "revision guard",
        "--force-current",
        "generated human",
        "no tasks",
    ):
        assert phrase in agents
    ignores = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".untaped/orchestration/**/.lock",
        ".untaped/orchestration/**/.DS_Store",
        ".untaped/orchestration/**/.*.untaped-tmp-*",
        ".untaped/orchestration/**/*~",
        ".untaped/orchestration/**/*.swp",
        ".untaped/orchestration/**/*.swo",
        ".untaped/orchestration/**/*.tmp",
        ".untaped/orchestration/**/.#*",
        ".untaped/orchestration/**/#*",
    } <= ignores
    workflow = (ROOT / ".github/workflows/orchestration.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in workflow
    assert "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e" in workflow
    assert 'version: "0.11.19"' in workflow
    prefix = "uvx --python 3.14 --from 'untaped-orchestration==0.1.0' "
    assert re.findall(r"^\s+run: (uvx .+)$", workflow, re.MULTILINE) == [
        f"{prefix}untaped-orchestration check --local",
        f"{prefix}untaped-orchestration fmt --check --local",
        f"{prefix}untaped-orchestration render --check",
    ]
    assert "uv sync" not in workflow and "PYTHONPATH" not in workflow
    for path in (
        ".untaped/orchestration/**",
        ".github/workflows/orchestration.yml",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/decisions.md",
        "docs/orchestration-adoption/**",
    ):
        assert path in workflow
