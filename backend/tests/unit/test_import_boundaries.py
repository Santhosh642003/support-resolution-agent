"""Import boundaries, enforced rather than remembered."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# httpx2 / httpcore2 arrive transitively via huggingface-hub (tokenizers). Our code uses httpx.
FORBIDDEN_TOP_LEVEL = {"httpx2", "httpcore2"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_no_module_under_app_imports_httpx2() -> None:
    offenders = {
        str(p.relative_to(APP)): sorted(_imported_modules(p) & FORBIDDEN_TOP_LEVEL)
        for p in APP.rglob("*.py")
        if _imported_modules(p) & FORBIDDEN_TOP_LEVEL
    }
    assert not offenders, f"forbidden imports under app/: {offenders}"


def test_the_checker_actually_detects_a_forbidden_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import httpx2\nfrom httpcore2.x import y\n")
    assert _imported_modules(bad) & FORBIDDEN_TOP_LEVEL == {"httpx2", "httpcore2"}
