import ast
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path

PACKAGE_NAME = "untaped_orchestration"
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "untaped_orchestration"
LAYERS = ("domain", "application", "infrastructure", "cli")
FORBIDDEN_LAYER_IMPORTS = {
    "domain": frozenset({"application", "infrastructure", "cli"}),
    "application": frozenset({"infrastructure", "cli"}),
    "infrastructure": frozenset({"cli"}),
    "cli": frozenset(),
}


@dataclass(frozen=True)
class ImportReference:
    module: str
    names: tuple[str, ...] = ()


def _package_context(path: Path, package_root: Path) -> str:
    relative_parts = path.relative_to(package_root).with_suffix("").parts
    package_parts = relative_parts[:-1]
    return ".".join((PACKAGE_NAME, *package_parts))


def _imported_modules(
    tree: ast.AST,
    *,
    path: Path,
    package_root: Path,
) -> list[ImportReference]:
    modules: list[ImportReference] = []
    package_context = _package_context(path, package_root)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(ImportReference(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            module = node.module
            if node.level:
                module = resolve_name(f"{'.' * node.level}{module}", package_context)
            modules.append(ImportReference(module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.ImportFrom) and node.level:
            module = resolve_name("." * node.level, package_context)
            modules.append(ImportReference(module, tuple(alias.name for alias in node.names)))
    return modules


def _source_layer(path: Path, package_root: Path) -> str:
    relative = path.relative_to(package_root)
    if len(relative.parts) > 1 and relative.parts[0] in LAYERS:
        return relative.parts[0]
    if relative.name == "__main__.py":
        return "cli"
    return "root"


def _imported_layers(reference: ImportReference) -> tuple[str, ...]:
    prefix = f"{PACKAGE_NAME}."
    if reference.module == PACKAGE_NAME:
        return tuple(name for name in reference.names if name in LAYERS)
    if reference.module.startswith(prefix):
        return (reference.module.removeprefix(prefix).split(".", maxsplit=1)[0],)
    return ()


def _is_sdk_import(reference: ImportReference) -> bool:
    return reference.module == "untaped" or reference.module.startswith("untaped.")


def _is_allowed_sdk_import(reference: ImportReference, source_layer: str) -> bool:
    return source_layer == "cli" and reference.module == "untaped.api"


def _is_application_ports_import(reference: ImportReference) -> bool:
    application = f"{PACKAGE_NAME}.application"
    if reference.module == f"{application}.ports":
        return True
    return reference.module == application and reference.names == ("ports",)


def scan_layer_violations(package_root: Path = PACKAGE_ROOT) -> list[str]:
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        source_layer = _source_layer(path, package_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for reference in _imported_modules(tree, path=path, package_root=package_root):
            if _is_sdk_import(reference):
                if not _is_allowed_sdk_import(reference, source_layer):
                    violations.append(
                        f"{path.relative_to(package_root)} imports SDK {reference.module}"
                    )
                continue
            for imported_layer in _imported_layers(reference):
                if imported_layer in FORBIDDEN_LAYER_IMPORTS.get(source_layer, ()):
                    violations.append(
                        f"{path.relative_to(package_root)} imports outward layer {imported_layer}"
                    )
                elif (
                    source_layer == "infrastructure"
                    and imported_layer == "application"
                    and not _is_application_ports_import(reference)
                ):
                    violations.append(
                        f"{path.relative_to(package_root)} imports application outside ports"
                    )
    return violations


def test_layers_point_inward_only() -> None:
    assert scan_layer_violations() == []


def test_scanner_rejects_every_forbidden_dependency(tmp_path: Path) -> None:
    package_root = tmp_path / "untaped_orchestration"
    forbidden_imports = {
        "domain/rules.py": (
            "untaped_orchestration.application",
            "untaped_orchestration.infrastructure",
            "untaped_orchestration.cli",
            "untaped.api",
        ),
        "application/use_case.py": (
            "untaped_orchestration.infrastructure",
            "untaped_orchestration.cli",
            "untaped.api",
        ),
        "infrastructure/repository.py": (
            "untaped_orchestration.cli",
            "untaped.api",
        ),
    }
    expected_count = 0
    for relative_path, modules in forbidden_imports.items():
        path = package_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"import {module}" for module in modules), encoding="utf-8")
        expected_count += len(modules)

    from_import = package_root / "domain" / "from_import.py"
    from_import.write_text(
        "from untaped_orchestration import application\n",
        encoding="utf-8",
    )
    expected_count += 1

    assert len(scan_layer_violations(package_root)) == expected_count


def test_scanner_rejects_relative_forbidden_dependencies(tmp_path: Path) -> None:
    package_root = tmp_path / "untaped_orchestration"
    relative_imports = {
        "domain/rules.py": (
            "from .. import application",
            "from .. import infrastructure",
            "from .. import cli",
        ),
        "application/use_case.py": (
            "from .. import infrastructure",
            "from .. import cli",
        ),
        "infrastructure/repository.py": ("from .. import cli",),
    }
    for relative_path, imports in relative_imports.items():
        path = package_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(imports), encoding="utf-8")

    assert len(scan_layer_violations(package_root)) == 6


def test_scanner_restricts_infrastructure_to_application_ports(tmp_path: Path) -> None:
    package_root = tmp_path / "untaped_orchestration"
    repository = package_root / "infrastructure" / "repository.py"
    repository.parent.mkdir(parents=True)
    repository.write_text(
        "import untaped_orchestration.application.use_case\n",
        encoding="utf-8",
    )

    assert len(scan_layer_violations(package_root)) == 1


def test_scanner_rejects_sdk_outside_composition_and_private_sdk_imports(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "untaped_orchestration"
    forbidden_imports = {
        "settings.py": "from untaped.api import create_app\n",
        "cli/private_sdk.py": "from untaped.cli import echo\n",
    }
    for relative_path, source in forbidden_imports.items():
        path = package_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    assert len(scan_layer_violations(package_root)) == 2


def test_scanner_allows_inward_dependencies_and_cli_composition(tmp_path: Path) -> None:
    package_root = tmp_path / "untaped_orchestration"
    allowed_imports = {
        "application/use_case.py": "untaped_orchestration.domain",
        "infrastructure/repository.py": "untaped_orchestration.application.ports",
        "cli/commands.py": "untaped_orchestration.infrastructure",
        "cli/api.py": "untaped.api",
        "__main__.py": "untaped.api",
    }
    for relative_path, module in allowed_imports.items():
        path = package_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"import {module}\n", encoding="utf-8")

    assert scan_layer_violations(package_root) == []
