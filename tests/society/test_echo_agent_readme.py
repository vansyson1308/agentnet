import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "examples" / "echo_agent" / "README.md"


def _text():
    assert README.exists(), "examples/echo_agent/README.md is missing"
    return README.read_text(encoding="utf-8")


def test_readme_exists_and_has_title():
    text = _text()
    assert text.lstrip().startswith("# ")
    assert len(text) >= 400


def test_readme_covers_staging_run_and_output():
    low = _text().lower()
    assert "staging" in low
    assert "python" in low
    assert "## run" in low or "## how to run" in low
    assert "expected output" in low or "successful run" in low


def test_readme_has_no_secrets():
    text = _text()
    assert "Bearer ey" not in text
    assert not re.search(r"0x[0-9a-fA-F]{40}", text)
