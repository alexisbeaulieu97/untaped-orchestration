from pathlib import Path

import pytest

from untaped_orchestration.domain.models import Registry, StoreConfig
from untaped_orchestration.infrastructure import codec as codec_module
from untaped_orchestration.infrastructure.codec import (
    CodecError,
    RegistryCodec,
    StoreConfigCodec,
)

STORE_ID = "sto_019f0000000070008000000000000000"
CHILD_A = "sto_019f0000000070008000000000000001"
CHILD_B = "sto_019f0000000070008000000000000002"
FIXTURES = Path(__file__).parents[2] / "fixtures" / "codec"


def store_toml() -> bytes:
    return (FIXTURES / "canonical-store.toml").read_bytes()


def registry_toml() -> bytes:
    return (FIXTURES / "canonical-registry.toml").read_bytes()


def test_store_codec_validates_full_shape_and_removes_comments_canonically() -> None:
    noncanonical = (
        b"# admin comment\n"
        b'name = "Untaped orchestration hub"\n'
        b'schema = "untaped.orchestration.store/v1"\n'
        + f'id = "{STORE_ID}"\n'.encode()
        + b'visibility = "private"\n'
        + b'timezone = "America/Montreal"\n'
        + b"\n[curation]\n"
        + b"in_progress_review_days = 14\ninbox_review_days = 7\n"
        + b"\n[capabilities]\nactive_tasks = true\n"
        + b"\n[brief]\n"
        + b"max_total_bytes = 32768\n"
        + b"max_rows_per_section = 10\n"
        + b"max_total_body_bytes = 16384\n"
        + b"max_decision_body_bytes = 4096\n"
        + b"pinned_decisions = []\n"
    )

    config = StoreConfigCodec().parse(noncanonical)
    canonical = StoreConfigCodec().canonical_bytes(config)

    assert isinstance(config, StoreConfig)
    assert canonical == store_toml()
    assert b"comment" not in canonical


def test_registry_codec_sorts_children_and_uses_fixed_array_table_order() -> None:
    noncanonical = (
        b"# registry comment\n"
        + f'store_id = "{STORE_ID}"\n'.encode()
        + b'schema = "untaped.orchestration.registry/v1"\n'
        + b"\n[[children]]\n"
        + b'path = "../../second/.untaped/orchestration"\n'
        + f'id = "{CHILD_B}"\n'.encode()
        + b"\n[[children]]\n"
        + f'id = "{CHILD_A}"\n'.encode()
        + b'path = "../../first/.untaped/orchestration"\n'
    )

    registry = RegistryCodec().parse(noncanonical)
    canonical = RegistryCodec().canonical_bytes(registry)

    assert isinstance(registry, Registry)
    assert canonical == registry_toml()
    assert canonical.index(CHILD_A.encode()) < canonical.index(CHILD_B.encode())
    assert b"comment" not in canonical


@pytest.mark.parametrize(
    ("codec", "raw"),
    [
        (StoreConfigCodec(), b'schema = "untaped.orchestration.store/v1"\n'),
        (
            RegistryCodec(),
            (
                'schema = "untaped.orchestration.registry/v1"\n'
                f'store_id = "{STORE_ID}"\n'
                "unknown = true\n"
            ).encode(),
        ),
    ],
)
def test_admin_codecs_reject_incomplete_or_unknown_shapes(
    codec: StoreConfigCodec | RegistryCodec,
    raw: bytes,
) -> None:
    with pytest.raises(CodecError) as captured:
        codec.parse(raw)

    assert captured.value.diagnostic.code == "ORC002"


def test_admin_codec_reports_duplicate_toml_key_location() -> None:
    duplicate = store_toml().replace(
        b'name = "Untaped orchestration hub"\n',
        b'name = "Untaped orchestration hub"\nname = "Duplicate"\n',
    )

    with pytest.raises(CodecError) as captured:
        StoreConfigCodec().parse(duplicate)

    assert captured.value.diagnostic.code == "ORC001"
    assert captured.value.diagnostic.line is not None
    assert captured.value.diagnostic.column is not None


def test_admin_serializers_reparse_and_revalidate_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = StoreConfigCodec().parse(store_toml())
    monkeypatch.setattr(codec_module.tomli_w, "dumps", lambda _value: 'schema = "invalid"\n')

    with pytest.raises(CodecError) as captured:
        StoreConfigCodec().canonical_bytes(config)

    assert captured.value.diagnostic.code == "ORC002"


@pytest.mark.parametrize(
    ("codec", "raw"),
    [(StoreConfigCodec(), store_toml()), (RegistryCodec(), registry_toml())],
)
def test_admin_canonicalization_is_idempotent(
    codec: StoreConfigCodec | RegistryCodec,
    raw: bytes,
) -> None:
    first = codec.canonical_bytes(codec.parse(raw))
    second = codec.canonical_bytes(codec.parse(first))

    assert second == first


@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf" + store_toml(), store_toml() + b"\xff"])
def test_admin_toml_rejects_bom_and_invalid_utf8(raw: bytes) -> None:
    with pytest.raises(CodecError) as captured:
        StoreConfigCodec().parse(raw)

    assert captured.value.diagnostic.code == "ORC001"
