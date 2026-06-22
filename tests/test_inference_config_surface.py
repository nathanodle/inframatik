import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _planner_get_keys(arg_name: str, function_names: set[str]) -> set[str]:
    tree = ast.parse((ROOT / "inference_planner.py").read_text())
    keys: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef):
            if node.name not in function_names:
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == arg_name
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
            self.generic_visit(node)

    Visitor().visit(tree)
    return keys


def _structured_keys(name: str) -> set[str]:
    app_js = (ROOT / "static/app.js").read_text()
    match = re.search(rf"const {re.escape(name)} = \[(.*?)\];", app_js, re.S)
    assert match, f"{name} was not found in static/app.js"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _structured_engine_keys(engine: str) -> set[str]:
    app_js = (ROOT / "static/app.js").read_text()
    match = re.search(
        rf"\b{re.escape(engine)}:\s*\[(.*?)\]\s*,",
        app_js,
        re.S,
    )
    assert match, f"{engine} structured engine keys were not found in static/app.js"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_planner_common_keys_are_structured_or_intentionally_external():
    planner_keys = _planner_get_keys(
        "common",
        {"_render_vllm", "_render_sglang", "_render_llama_cpp", "_render_common_openai_flags", "_profile_uses_api_key"},
    )
    structured = _structured_keys("STRUCTURED_COMMON_KEYS")
    external = {
        "api_key",  # Raw engine keys are managed through the one-time secret lifecycle.
        "api_key_enabled",  # Compatibility alias for secret-backed API key enablement.
        "engine_api_key",  # Legacy secret-backed API key alias.
        "host",  # Resolved per instance by the planner.
        "port",  # Resolved by deployment port policy / instance allocation.
    }
    missing = sorted(planner_keys - structured - external)
    assert missing == []


def test_planner_engine_keys_are_exposed_as_structured_editor_keys():
    renderers = {
        "vllm": "_render_vllm",
        "sglang": "_render_sglang",
        "llama_cpp": "_render_llama_cpp",
    }
    for engine, renderer in renderers.items():
        planner_keys = _planner_get_keys("cfg", {renderer})
        structured = _structured_engine_keys(engine)
        missing = sorted(planner_keys - structured)
        assert missing == [], f"{engine} planner cfg keys missing from STRUCTURED_ENGINE_KEYS: {missing}"
