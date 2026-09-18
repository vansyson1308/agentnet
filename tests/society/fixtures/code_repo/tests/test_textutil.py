from app.textutil import normalize_whitespace, parse_bool, slugify


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("") == "item"


def test_parse_bool_canonical_values():
    assert parse_bool("true") is True
    assert parse_bool("1") is True
    assert parse_bool("false") is False
    assert parse_bool("0") is False
    assert parse_bool(True) is True


def test_normalize_whitespace():
    assert normalize_whitespace("  a \t b\n c ") == "a b c"
