from pathlib import Path

STORE_ID = "sto_019f0000000070008000000000000000"
CHILD_STORE_ID = "sto_019f0000000070008000000000000001"
TASK_ID = "tsk_019f0000000070008000000000000010"
DECISION_ID = "dec_019f0000000070008000000000000001"

FIXTURES = Path(__file__).parent / "fixtures" / "codec"


def store_bytes(*, store_id: str = STORE_ID) -> bytes:
    return (
        (FIXTURES / "canonical-store.toml")
        .read_bytes()
        .replace(STORE_ID.encode(), store_id.encode())
    )


def registry_bytes(*, store_id: str = STORE_ID) -> bytes:
    return b'schema = "untaped.orchestration.registry/v1"\n' + f'store_id = "{store_id}"\n'.encode()


def task_bytes() -> bytes:
    return (FIXTURES / "canonical-task.md").read_bytes()


def decision_bytes() -> bytes:
    return (FIXTURES / "canonical-decision.md").read_bytes()


def store_root(repository: Path) -> Path:
    return repository / ".untaped" / "orchestration"


def write_store(repository: Path, *, store_id: str = STORE_ID) -> Path:
    root = store_root(repository)
    root.mkdir(parents=True)
    root.joinpath("store.toml").write_bytes(store_bytes(store_id=store_id))
    root.joinpath("registry.toml").write_bytes(registry_bytes(store_id=store_id))
    root.joinpath("AGENTS.md").write_bytes(b"Use the orchestration CLI.\n")
    root.joinpath("CLAUDE.md").write_bytes(b"@AGENTS.md\n")
    return root
