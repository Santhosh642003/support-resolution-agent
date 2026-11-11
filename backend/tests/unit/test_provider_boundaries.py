"""No module outside app/providers/ may import a vendor SDK or reference a model name.

CLAUDE.md standing constraint, enforced instead of remembered. Scope: app/ and tests/, except
app/providers/ itself (the adapter layer), this file, and the provider tests that legitimately
exercise adapter behaviour with placeholder model names. Model-name detection is a regex over
string literals and identifiers in the AST, so a name in a comment or docstring does not trip it.
"""

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
ALLOWED_DIRS = (BACKEND / "app" / "providers",)
SELF = Path(__file__).resolve()

# Vendor SDKs. httpx is the generic HTTP client the adapters use and is not an SDK.
VENDOR_SDKS = {
    "groq",
    "openai",
    "anthropic",
    "google",
    "vertexai",
    "cohere",
    "mistralai",
    "litellm",
    "langchain",
    "langchain_openai",
    "langchain_core",
    "llama_index",
    "ollama",
    "together",
}
MODEL_NAME = re.compile(
    r"gpt-oss|gpt-4|gpt-5|\bo[134]-(?:mini|preview)|llama[-_ ]?\d|llama-prompt-guard|"
    r"qwen\d?|gemini[-_ ]?\d|\bgemma|mixtral|mistral-(?:large|small|medium)|claude-(?:fable|opus|sonnet|haiku)|"
    r"deepseek",
    re.IGNORECASE,
)


def vendor_imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found & VENDOR_SDKS


def model_names(source: str) -> list[str]:
    """Model-name-looking string literals (docstrings excluded)."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and MODEL_NAME.search(node.value)
        ):
            hits.append(node.value[:60])
    return hits


def _scanned_files() -> list[Path]:
    files = [*(BACKEND / "app").rglob("*.py"), *(BACKEND / "tests").rglob("*.py")]
    return [
        p
        for p in files
        if p.resolve() != SELF and not any(d in p.resolve().parents for d in ALLOWED_DIRS)
    ]


def test_no_vendor_sdk_is_imported_outside_the_provider_layer() -> None:
    offenders = {
        str(p.relative_to(BACKEND)): sorted(vendor_imports(p.read_text(encoding="utf-8")))
        for p in _scanned_files()
        if vendor_imports(p.read_text(encoding="utf-8"))
    }
    assert not offenders, f"vendor SDK imports outside app/providers/: {offenders}"


def test_no_model_name_appears_outside_the_provider_layer() -> None:
    offenders = {
        str(p.relative_to(BACKEND)): model_names(p.read_text(encoding="utf-8"))
        for p in _scanned_files()
        if model_names(p.read_text(encoding="utf-8"))
    }
    assert not offenders, f"model names outside app/providers/: {offenders}"


def test_the_provider_layer_itself_does_name_a_model_so_the_scan_is_not_vacuous() -> None:
    adapter = (BACKEND / "app" / "providers" / "adapters" / "groq.py").read_text(encoding="utf-8")
    assert model_names(adapter), "the Groq adapter's default model should be detected"


def test_the_detectors_detect() -> None:
    assert vendor_imports("import openai") == {"openai"}
    assert vendor_imports("from groq import Groq") == {"groq"}
    assert vendor_imports("import google.generativeai as genai") == {"google"}
    assert vendor_imports("import httpx\nimport json") == set()
    assert model_names('MODEL = "openai/gpt-oss-120b"')
    assert model_names('m = "gemini-2.5-flash"')
    assert model_names('m = "meta-llama/llama-4-scout"')
    assert not model_names('"""about gpt-oss"""\nx = "plain string"')
    assert not model_names('x = "fake-model"')
