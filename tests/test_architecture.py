"""Guard module boundaries that keep headless ASR and text processing independently usable."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "talk2g"


def import_graph() -> dict[str, set[str]]:
    graph = {}
    for path in PACKAGE.rglob("*.py"):
        parts = path.relative_to(PACKAGE).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(("talk2g", *(parts[:-1] if is_package else parts)))
        package = module if is_package else module.rpartition(".")[0]
        imports = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = resolve_name("." * node.level + base, package)
                imports.add(base)
                imports.update(f"{base}.{alias.name}" for alias in node.names)
        graph[module] = imports
    return graph


def reachable_imports(graph: dict[str, set[str]], module: str) -> set[str]:
    reached = set()
    pending = [module]
    while pending:
        for dependency in graph.get(pending.pop(), ()):
            if dependency not in reached:
                reached.add(dependency)
                pending.append(dependency)
    return reached


@pytest.mark.parametrize(
    "module,forbidden",
    [
        ("talk2g.server", ("PySide6", "talk2g.desktop", "talk2g.client", "talk2g.ui")),
        ("talk2g.dictated_text", ("PySide6", "talk2g.model", "talk2g.server", "talk2g.client")),
        (
            "talk2g.ui",
            ("talk2g.desktop", "talk2g.client", "talk2g.server", "talk2g.service", "talk2g.insertion"),
        ),
    ],
    ids=["headless-server", "independent-client-text", "views-without-controller"],
)
def test_module_dependencies_respect_architecture_boundaries(module, forbidden):
    # GIVEN: все явные импорты, включая отложенные внутри функций.
    graph = import_graph()
    roots = [name for name in graph if name == module or name.startswith(module + ".")]
    assert roots, f"Не найден проверяемый модуль {module}"
    # WHEN: прослеживаем зависимости, включая транзитивные.
    imports = set().union(*(reachable_imports(graph, root) for root in roots))
    violations = sorted(
        name
        for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    )
    # THEN: распознавание и текст работают без GUI, представления не управляют службами.
    assert not violations, f"{module}: недопустимые зависимости {violations}"


def test_internal_modules_have_no_circular_dependencies():
    # GIVEN: граф импортов всех модулей приложения.
    graph = import_graph()
    # WHEN: проверяем транзитивный путь обратно к каждому модулю.
    cycles = sorted(module for module in graph if module in reachable_imports(graph, module))
    # THEN: контроллеры, представления и обработка данных не образуют циклов.
    assert cycles == [], f"Циклические зависимости: {cycles}"
