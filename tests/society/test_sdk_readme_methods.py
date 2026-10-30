from pathlib import Path

README = Path(__file__).resolve().parents[2] / "sdk" / "python" / "README.md"

METHODS = [
    "approve",
    "confirm_task",
    "create_approval",
    "deny",
    "dev_fund_wallet",
]


def _text() -> str:
    return README.read_text(encoding="utf-8")


def test_each_method_has_a_call_example():
    text = _text()
    missing = [m for m in METHODS if f".{m}(" not in text]
    assert not missing, f"SDK README lacks examples for: {missing}"


def test_examples_are_inside_python_fences():
    text = _text()
    blocks = text.split("```python")[1:]
    code = "\n".join(b.split("```")[0] for b in blocks)
    missing = [m for m in METHODS if f".{m}(" not in code]
    assert not missing, f"not in a python code block: {missing}"
