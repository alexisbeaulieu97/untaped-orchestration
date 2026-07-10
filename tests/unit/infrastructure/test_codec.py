from pathlib import Path, PurePosixPath

import pytest

from untaped_orchestration.domain.models import ActiveTask, ArchivedTask, Decision
from untaped_orchestration.infrastructure.codec import CodecError, ItemCodec

STORE_ID = "sto_019f0000000070008000000000000000"
TASK_ID = "tsk_019f0000000070008000000000000010"
OTHER_TASK_ID = "tsk_019f0000000070008000000000000011"
DECISION_ID = "dec_019f0000000070008000000000000001"
OTHER_DECISION_ID = "dec_019f0000000070008000000000000002"
TIMESTAMP = "2026-07-10T01:02:03.004Z"
UTF8_BOM = b"\xef\xbb\xbf"
FIXTURES = Path(__file__).parents[2] / "fixtures" / "codec"


def task_metadata(*, archived: bool = False) -> bytes:
    lifecycle = (
        b'priority = "normal"\n'
        b"rank = 1000\n"
        b"waiting_on = []\n"
        b'closed_from = "inbox"\n'
        b'outcome = "declined"\n'
        b'closed_at = "2026-07-10T01:02:03.004Z"\n'
        b'close_note = "No longer needed"\n'
        if archived
        else b'stage = "inbox"\npriority = "normal"\nrank = 1000\nwaiting_on = []\n'
    )
    return (
        b'schema = "untaped.orchestration.task/v1"\n'
        + f'id = "{TASK_ID}"\n'.encode()
        + b'kind = "task"\n'
        + b'title = "Land the public orchestration specification"\n'
        + f'created_at = "{TIMESTAMP}"\n'.encode()
        + b'tags = ["orchestration", "specification"]\n'
        + lifecycle
    )


def decision_metadata() -> bytes:
    return (
        b'schema = "untaped.orchestration.decision/v1"\n'
        + f'id = "{DECISION_ID}"\n'.encode()
        + b'kind = "decision"\n'
        + b'title = "Use TOML front matter and opaque Markdown bodies"\n'
        + f'created_at = "{TIMESTAMP}"\n'.encode()
        + b'tags = ["format", "orchestration"]\n'
    )


def envelope(metadata: bytes, body: bytes = b"") -> bytes:
    return b"+++\n" + metadata + b"+++\n" + body


def diagnostic_for(raw: bytes, *, path: PurePosixPath) -> CodecError:
    with pytest.raises(CodecError) as captured:
        ItemCodec().parse(raw, relative_path=path)
    return captured.value


def test_parses_valid_task_and_decision_golden_files_as_exact_documents() -> None:
    task_raw = (FIXTURES / "canonical-task.md").read_bytes()
    decision_raw = (FIXTURES / "canonical-decision.md").read_bytes()
    codec = ItemCodec()

    task = codec.parse(
        task_raw,
        relative_path=PurePosixPath(f"tasks/{TASK_ID}-land-specification.md"),
    )
    decision = codec.parse(
        decision_raw,
        relative_path=PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md"),
    )

    assert isinstance(task.metadata, ActiveTask)
    assert isinstance(decision.metadata, Decision)
    assert task.original == task_raw
    assert task.body == b"## Context\n\nOpaque Markdown body.\n"
    assert decision.original == decision_raw
    assert decision.body == b"The envelope is machine-owned.\n"
    assert codec.canonical_bytes(task) == task_raw
    assert codec.canonical_bytes(decision) == decision_raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xef\xbb\xbf" + envelope(decision_metadata()), "byte-order mark"),
        (envelope(decision_metadata()) + b"\xff", "UTF-8"),
        (decision_metadata(), "opening delimiter"),
        (b"++++\n" + decision_metadata() + b"+++\n", "opening delimiter"),
        (b"+++\n" + decision_metadata(), "closing delimiter"),
        (b"+++\r\n" + decision_metadata() + b"+++\n", "opening delimiter"),
    ],
)
def test_rejects_bom_invalid_utf8_and_missing_or_malformed_delimiters(
    raw: bytes,
    message: str,
) -> None:
    error = diagnostic_for(
        raw,
        path=PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md"),
    )

    assert error.diagnostic.code == "ORC001"
    assert message in error.diagnostic.message


def test_reports_duplicate_key_and_toml_syntax_locations_in_file_coordinates() -> None:
    duplicate = decision_metadata() + b'title = "Duplicate"\n'
    malformed = decision_metadata() + b"review_on = \n"
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    duplicate_error = diagnostic_for(envelope(duplicate), path=path)
    syntax_error = diagnostic_for(envelope(malformed), path=path)

    assert duplicate_error.diagnostic.code == "ORC001"
    assert duplicate_error.diagnostic.line == 8
    assert duplicate_error.diagnostic.column is not None
    assert syntax_error.diagnostic.code == "ORC001"
    assert syntax_error.diagnostic.line == 8
    assert syntax_error.diagnostic.column is not None


def test_enforces_frontmatter_and_body_byte_bounds_at_the_exact_boundaries() -> None:
    base = decision_metadata()
    exact_metadata = base + b"#" + b"x" * (64 * 1024 - len(base) - 2) + b"\n"
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    exact = ItemCodec().parse(envelope(exact_metadata, b"b" * (1024 * 1024)), relative_path=path)
    assert len(exact.body) == 1024 * 1024

    frontmatter_error = diagnostic_for(envelope(exact_metadata + b"#\n"), path=path)
    body_error = diagnostic_for(envelope(base, b"b" * (1024 * 1024 + 1)), path=path)
    assert frontmatter_error.diagnostic.code == "ORC001"
    assert "64 KiB" in frontmatter_error.diagnostic.message
    assert body_error.diagnostic.code == "ORC001"
    assert "1 MiB" in body_error.diagnostic.message


def test_bounded_splitter_stops_an_unterminated_line_at_the_frontmatter_limit() -> None:
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    error = diagnostic_for(b"+++\n" + b"x" * (2 * 64 * 1024), path=path)

    assert "64 KiB" in error.diagnostic.message


@pytest.mark.parametrize(
    ("metadata", "field"),
    [
        (decision_metadata() + b"unknown = true\n", "decision.unknown"),
        (decision_metadata().replace(b'kind = "decision"', b'kind = "task"'), "task"),
        (
            decision_metadata().replace(
                b"untaped.orchestration.decision/v1",
                b"untaped.orchestration.task/v1",
            ),
            "decision.schema",
        ),
    ],
)
def test_rejects_unknown_fields_and_schema_kind_mismatches(
    metadata: bytes,
    field: str,
) -> None:
    error = diagnostic_for(
        envelope(metadata),
        path=PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md"),
    )

    assert error.diagnostic.code == "ORC002"
    assert field in error.diagnostic.field


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (PurePosixPath(f"tasks/{OTHER_TASK_ID}-land-specification.md"), "id"),
        (PurePosixPath(f"tasks/{TASK_ID}-Bad_Slug.md"), "slug"),
        (PurePosixPath(f"tasks/{TASK_ID}-{'a' * 65}.md"), "slug"),
        (PurePosixPath("tasks/not-a-canonical-item.md"), "filename"),
    ],
)
def test_filename_diagnostics_distinguish_grammar_slug_and_identity(
    path: PurePosixPath,
    field: str,
) -> None:
    error = diagnostic_for(envelope(task_metadata()), path=path)

    assert error.diagnostic.code == "ORC003"
    assert error.diagnostic.field == field


def test_accepts_only_the_exact_placement_for_each_item_shape() -> None:
    codec = ItemCodec()

    active = codec.parse(
        envelope(task_metadata()),
        relative_path=PurePosixPath(f"tasks/{TASK_ID}-land-specification.md"),
    )
    archived = codec.parse(
        envelope(task_metadata(archived=True)),
        relative_path=PurePosixPath(f"archive/tasks/{TASK_ID}-land-specification.md"),
    )
    decision = codec.parse(
        envelope(decision_metadata()),
        relative_path=PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md"),
    )

    assert isinstance(active.metadata, ActiveTask)
    assert isinstance(archived.metadata, ArchivedTask)
    assert isinstance(decision.metadata, Decision)


@pytest.mark.parametrize(
    ("metadata", "path"),
    [
        (
            task_metadata(),
            PurePosixPath(f"archive/tasks/{TASK_ID}-land-specification.md"),
        ),
        (task_metadata(), PurePosixPath(f"decisions/{TASK_ID}-land-specification.md")),
        (task_metadata(archived=True), PurePosixPath(f"tasks/{TASK_ID}-land-specification.md")),
        (decision_metadata(), PurePosixPath(f"tasks/{DECISION_ID}-toml-envelope.md")),
        (task_metadata(), PurePosixPath(f"tasks/nested/{TASK_ID}-land-specification.md")),
        (task_metadata(), PurePosixPath(f"/tasks/{TASK_ID}-land-specification.md")),
        (task_metadata(), PurePosixPath(f"tasks/../tasks/{TASK_ID}-land-specification.md")),
    ],
)
def test_rejects_unsafe_or_nonexact_item_placement(metadata: bytes, path: PurePosixPath) -> None:
    error = diagnostic_for(envelope(metadata), path=path)

    assert error.diagnostic.code == "ORC003"
    assert error.diagnostic.field == "path"


def test_replacement_frontmatter_applies_the_same_exact_placement_validation() -> None:
    with pytest.raises(CodecError) as captured:
        ItemCodec().parse_replacement_frontmatter(
            task_metadata(),
            relative_path=PurePosixPath(f"archive/tasks/{TASK_ID}-land-specification.md"),
        )

    assert captured.value.diagnostic.code == "ORC003"
    assert captured.value.diagnostic.field == "path"


def test_enveloped_metadata_leading_ufeff_reports_full_file_location() -> None:
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    error = diagnostic_for(envelope(UTF8_BOM + decision_metadata()), path=path)

    assert error.diagnostic.code == "ORC001"
    assert error.diagnostic.line == 2
    assert error.diagnostic.column == 1
    assert error.diagnostic.byte_offset == 4


def test_replacement_metadata_leading_ufeff_reports_raw_toml_location() -> None:
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    with pytest.raises(CodecError) as captured:
        ItemCodec().parse_replacement_frontmatter(
            UTF8_BOM + decision_metadata(),
            relative_path=path,
        )

    assert captured.value.diagnostic.line == 1
    assert captured.value.diagnostic.column == 1
    assert captured.value.diagnostic.byte_offset == 0


def test_canonicalization_preserves_every_body_byte_and_treats_later_delimiters_as_body() -> None:
    body = b"## Context\r\n\r\n+++\r\ninline +++ marker\r\n+++\nno-final-newline"
    noncanonical = (
        b"+++\n"
        b"# metadata comment\n"
        b'kind = "task"\n'
        + f'id = "{TASK_ID}"\n'.encode()
        + b'schema = "untaped.orchestration.task/v1"\n'
        + b'title = "Land the public orchestration specification"\n'
        + f'created_at = "{TIMESTAMP}"\n'.encode()
        + b'tags = ["zeta", "alpha"]\n'
        + b'priority = "normal"\nrank = 1000\nstage = "inbox"\nwaiting_on = []\n'
        + b"+++\n"
        + body
    )
    path = PurePosixPath(f"tasks/{TASK_ID}-land-specification.md")

    document = ItemCodec().parse(noncanonical, relative_path=path)
    canonical = ItemCodec().canonical_bytes(document)
    reparsed = ItemCodec().parse(canonical, relative_path=path)

    assert canonical.endswith(body)
    assert reparsed.body == body
    assert not canonical.endswith(b"\n")
    assert b"# metadata comment" not in canonical
    assert b'schema = "untaped.orchestration.task/v1"' in canonical
    assert b"schema_" not in canonical


def test_canonicalization_allows_a_utf8_bom_codepoint_inside_the_opaque_body() -> None:
    body = b"\xef\xbb\xbfbody"
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    canonical = ItemCodec().canonical_bytes(
        ItemCodec().parse(envelope(decision_metadata(), body), relative_path=path)
    )

    assert canonical.endswith(body)


def test_canonical_frontmatter_has_fixed_order_and_sorted_collections() -> None:
    raw = envelope(
        task_metadata().replace(b'["orchestration", "specification"]', b'["zeta", "alpha"]')
        + f"""
parent = "{OTHER_TASK_ID}"

[[links]]
relation = "governed-by"
target_store_id = "{STORE_ID}"
target = "{DECISION_ID}"

[[links]]
relation = "depends-on"
target_store_id = "{STORE_ID}"
target = "{OTHER_TASK_ID}"

[[evidence]]
relation = "tracked-by"
reference = "github-pr:Owner/Repo#2"

[[evidence]]
relation = "implemented-by"
reference = "github-commit:Owner/Repo@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".encode()
    )
    path = PurePosixPath(f"tasks/{TASK_ID}-land-specification.md")

    frontmatter = (
        ItemCodec()
        .canonical_bytes(ItemCodec().parse(raw, relative_path=path))
        .split(
            b"+++\n",
            maxsplit=2,
        )[1]
    )

    ordered_tokens = [
        b"schema = ",
        b"id = ",
        b"kind = ",
        b"title = ",
        b"created_at = ",
        b"tags = ",
        b"stage = ",
        b"priority = ",
        b"rank = ",
        b"parent = ",
        b"waiting_on = ",
        b"[[links]]",
        b'relation = "depends-on"',
        b'relation = "governed-by"',
        b"[[evidence]]",
        b'relation = "implemented-by"',
        b'relation = "tracked-by"',
    ]
    positions = [frontmatter.index(token) for token in ordered_tokens]
    assert positions == sorted(positions)
    assert frontmatter.index(b'"alpha"') < frontmatter.index(b'"zeta"')
    for absent in (b"started_at", b"revisit_when", b"reviewed_at", b"review_on"):
        assert absent not in frontmatter


@pytest.mark.parametrize(
    ("raw", "path", "model_type"),
    [
        (
            envelope(task_metadata()),
            PurePosixPath(f"tasks/{TASK_ID}-land-specification.md"),
            ActiveTask,
        ),
        (
            envelope(task_metadata(archived=True)),
            PurePosixPath(f"archive/tasks/{TASK_ID}-land-specification.md"),
            ArchivedTask,
        ),
        (
            envelope(decision_metadata()),
            PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md"),
            Decision,
        ),
    ],
)
def test_canonicalization_is_idempotent_for_every_item_model_shape(
    raw: bytes,
    path: PurePosixPath,
    model_type: type[ActiveTask] | type[ArchivedTask] | type[Decision],
) -> None:
    codec = ItemCodec()
    first = codec.canonical_bytes(codec.parse(raw, relative_path=path))
    second_document = codec.parse(first, relative_path=path)

    assert isinstance(second_document.metadata, model_type)
    assert codec.canonical_bytes(second_document) == first


def test_replacement_frontmatter_is_bounded_validated_and_filename_checked() -> None:
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    parsed = ItemCodec().parse_replacement_frontmatter(decision_metadata(), relative_path=path)

    assert isinstance(parsed, Decision)
    with pytest.raises(CodecError, match="64 KiB"):
        ItemCodec().parse_replacement_frontmatter(b"#" * (64 * 1024 + 1), relative_path=path)
    with pytest.raises(CodecError) as mismatch:
        ItemCodec().parse_replacement_frontmatter(
            decision_metadata(),
            relative_path=PurePosixPath(f"decisions/{OTHER_DECISION_ID}-toml-envelope.md"),
        )
    assert mismatch.value.diagnostic.code == "ORC003"


def test_replacement_frontmatter_syntax_location_is_relative_to_replacement_bytes() -> None:
    path = PurePosixPath(f"decisions/{DECISION_ID}-toml-envelope.md")

    with pytest.raises(CodecError) as captured:
        ItemCodec().parse_replacement_frontmatter(b"schema = \n", relative_path=path)

    assert captured.value.diagnostic.line == 1
    assert captured.value.diagnostic.column is not None
