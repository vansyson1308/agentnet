from pathlib import Path

README = Path(__file__).resolve().parents[2] / "sdk" / "python" / "README.md"

METHODS = [
    "fail_task",
    "fetch_remote_agent_card",
    "get_agent_by_name",
    "get_agent_card",
    "get_approval",
]


def _text() -> str:
    assert README.exists(), "sdk/python/README.md must exist"
    return README.read_text(encoding="utf-8")


def test_each_method_is_documented_with_a_call_example():
    text = _text()
    for name in METHODS:
        assert name + "(" in text, name + " needs a call example in the README"


def test_readme_has_lookup_section():
    assert "## Task and agent lookup methods" in _text()


def test_readme_has_no_obvious_secrets():
    text = _text()
    assert "Bearer ey" not in text
    assert "sk-" not in text
