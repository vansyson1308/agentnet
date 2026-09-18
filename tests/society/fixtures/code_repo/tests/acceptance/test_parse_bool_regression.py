"""Regression for the reported task failures: affirmative words must parse as True."""

from app.textutil import parse_bool


def test_parse_bool_accepts_affirmative_words():
    assert parse_bool("yes") is True
    assert parse_bool("on") is True
    assert parse_bool("Y") is True


def test_parse_bool_still_rejects_negatives():
    assert parse_bool("no") is False
    assert parse_bool("off") is False
    assert parse_bool("maybe") is False
