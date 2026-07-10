import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "untaped_orchestration"
LAYERS = ("domain", "application", "infrastructure", "cli")
FORBIDDEN_LAYER_IMPORTS = {
    "domain": frozenset({"application", "infrastructure", "cli"}),
    "application": frozenset({"infrastructure", "cli"}),
    "infrastructure": frozenset({"cli"}),
    "cli": frozenset(),
}


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
            if node.module == "untaped_orchestration":
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _source_layer(path: Path, package_root: Path) -> str | None:
    relative = path.relative_to(package_root)
    if len(relative.parts) > 1 and relative.parts[0] in LAYERS:
        return relative.parts[0]
    if relative.name == "__main__.py":
        return "cli"
    return None


def scan_layer_violations(package_root: Path = PACKAGE_ROOT) -> list[str]:
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        source_layer = _source_layer(path, package_root)
        if source_layer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module == "untaped" or module.startswith("untaped."):
                if source_layer != "cli":
                    violations.append(f"{path.relative_to(package_root)} imports SDK {module}")
                continue
            prefix = "untaped_orchestration."
            if not module.startswith(prefix):
                continue
            imported_layer = module.removeprefix(prefix).split(".", maxsplit=1)[0]
            if imported_layer in FORBIDDEN_LAYER_IMPORTS[source_layer]:
                violations.append(
                    f"{path.relative_to(package_root)} imports outward layer {imported_layer}"
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


def test_scanner_allows_inward_dependencies_and_cli_composition(tmp_path: Path) -> None:
    package_root = tmp_path / "untaped_orchestration"
    allowed_imports = {
        "application/use_case.py": "untaped_orchestration.domain",
        "infrastructure/repository.py": "untaped_orchestration.application.ports",
        "cli/commands.py": "untaped_orchestration.infrastructure",
        "__main__.py": "untaped.api",
    }
    for relative_path, module in allowed_imports.items():
        path = package_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"import {module}\n", encoding="utf-8")

    assert scan_layer_violations(package_root) == []
