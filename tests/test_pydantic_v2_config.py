"""
Pydantic V2 configuration (Phase 2.6 §6.2).

Every active request/response model used to carry a V1-style inner
``class Config`` (``from_attributes = True`` or the removed ``orm_mode``),
which Pydantic 2.13 reports as ``PydanticDeprecatedSince20`` (and, for
``orm_mode``, a "Valid config keys have changed" warning). They now declare
``model_config = ConfigDict(from_attributes=True)``.

The check is behavioural, not just an import: for EVERY model that declares
``from_attributes`` an attribute-style object (what SQLAlchemy rows are) is
synthesised from the model's own field annotations and validated with
``model_validate`` — the ORM serialisation path the routes rely on.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import importlib
import inspect
import os
import pathlib
import sys
import types
import typing
import uuid

import pytest
from pydantic import BaseModel, EmailStr

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JAEGER_ENABLED", "false")

REPO = pathlib.Path(__file__).resolve().parent.parent
# The registry's route modules import their siblings as ``app.*`` (how the
# service runs); make that importable exactly like tests/society/conftest.py.
if str(REPO / "services" / "registry") not in sys.path:
    sys.path.insert(0, str(REPO / "services" / "registry"))
MODULES = [
    "services.registry.app.schemas",
    "services.payment.app.schemas",
    "services.simulation.app.schemas",
    "app.api.routes.chat",
    "app.api.routes.stories",
    "app.api.routes.notifications",
]


def _models(module_name):
    mod = importlib.import_module(module_name)
    out = []
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, BaseModel) and obj is not BaseModel and obj.__module__ == mod.__name__:
            out.append(obj)
    return out


ALL_MODELS = [m for name in MODULES for m in _models(name)]
ORM_MODELS = [m for m in ALL_MODELS if m.model_config.get("from_attributes")]


def _sample(annotation, depth=0):
    """A value satisfying ``annotation`` (only the types the schemas use)."""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is typing.Annotated:
        return _sample(args[0], depth)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        return None if len(non_none) < len(args) else _sample(non_none[0], depth)
    if origin is typing.Literal:
        return args[0]
    if origin in (list, typing.List, set, frozenset, tuple):
        return [] if origin in (list, typing.List) else origin()
    if origin in (dict, typing.Dict):
        return {}
    if annotation is typing.Any:
        return "any"
    if annotation is EmailStr:
        return "user@example.com"
    if inspect.isclass(annotation):
        if issubclass(annotation, BaseModel):
            return _namespace(annotation, depth + 1)
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation))
        if issubclass(annotation, bool):
            return True
        if issubclass(annotation, int):
            return 1
        if issubclass(annotation, float):
            return 1.0
        if issubclass(annotation, decimal.Decimal):
            return decimal.Decimal("1")
        if issubclass(annotation, dt.datetime):
            return dt.datetime.now(dt.timezone.utc)
        if issubclass(annotation, dt.date):
            return dt.date.today()
        if issubclass(annotation, uuid.UUID):
            return uuid.uuid4()
        if issubclass(annotation, str):
            return "user@example.com"  # also satisfies EmailStr
        if annotation in (dict,):
            return {}
        if annotation in (list,):
            return []
    raise AssertionError(f"no sample generator for annotation {annotation!r} — extend _sample()")


def _namespace(model, depth=0):
    assert depth < 6, f"nested models too deep for {model}"
    values = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        values[name] = _sample(field.annotation, depth)
    return types.SimpleNamespace(**values)


def test_models_were_collected():
    assert len(ALL_MODELS) >= 60
    assert len(ORM_MODELS) >= 33  # the 33 inner Config classes that were migrated


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: f"{m.__module__}.{m.__name__}")
def test_no_v1_inner_config_class(model):
    assert "Config" not in model.__dict__, f"{model.__name__} still declares a V1 inner Config class"
    assert "orm_mode" not in model.model_config, "orm_mode was removed in V2; use from_attributes"


@pytest.mark.parametrize("model", ORM_MODELS, ids=lambda m: f"{m.__module__}.{m.__name__}")
def test_from_attributes_serialises_orm_style_objects(model):
    assert model.model_config.get("from_attributes") is True
    row = _namespace(model)
    instance = model.model_validate(row)  # attribute access, exactly what SQLAlchemy rows offer
    dumped = instance.model_dump()
    for name, field in model.model_fields.items():
        if field.is_required():
            assert name in dumped
    # and a plain dict of the same data must still validate (API request path)
    model.model_validate(instance.model_dump())


def test_no_inner_config_class_left_in_active_services():
    offenders = []
    for path in (REPO / "services").rglob("*.py"):
        if "legacy" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip() == "class Config:" or "orm_mode" in line:
                offenders.append(f"{path.relative_to(REPO)}:{i}")
    assert offenders == []
