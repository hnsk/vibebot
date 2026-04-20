"""Helpers for per-module Pydantic settings."""

from __future__ import annotations

from typing import Any, get_args, get_origin

from pydantic import BaseModel, SecretStr

SECRET_PLACEHOLDER = "***"


class ModuleSettingsError(ValueError):
    """Raised for bad segments, validation failures, or unknown modules."""


def sanitize_segment(segment: str) -> str:
    """Reject path segments that could escape the per-module sandbox.

    Repo/module names come from the DB (not raw user request params), but the
    DB is populated from the config.toml file and the modules API, so a
    hostile value could still land there. Belt-and-braces: never allow path
    separators, parent refs, null bytes, empty strings, or leading dots.
    """
    if not isinstance(segment, str) or not segment:
        raise ModuleSettingsError("path segment must be a non-empty string")
    if segment in {".", ".."}:
        raise ModuleSettingsError(f"path segment {segment!r} not allowed")
    if segment.startswith("."):
        raise ModuleSettingsError(f"path segment may not start with '.': {segment!r}")
    for bad in ("/", "\\", "\x00"):
        if bad in segment:
            raise ModuleSettingsError(f"path segment contains {bad!r}: {segment!r}")
    return segment


def _field_is_secret(annotation: Any) -> bool:
    """True if a Pydantic field annotation resolves to SecretStr anywhere."""
    if annotation is SecretStr:
        return True
    origin = get_origin(annotation)
    if origin is None:
        try:
            return isinstance(annotation, type) and issubclass(annotation, SecretStr)
        except TypeError:
            return False
    return any(_field_is_secret(arg) for arg in get_args(annotation))


def secret_field_names(cls: type[BaseModel]) -> set[str]:
    return {name for name, field in cls.model_fields.items() if _field_is_secret(field.annotation)}


def dump_for_storage(model: BaseModel) -> dict[str, Any]:
    """JSON-safe dump with SecretStr unmasked — used when persisting to SQLite.

    ``BaseModel.model_dump(mode="json")`` masks SecretStr as ``"**********"``,
    which would silently clobber the stored secret. This helper round-trips
    through ``mode="python"`` and then JSON-safe-coerces each field, pulling
    the real value out of any SecretStr.
    """
    raw = model.model_dump(mode="python")
    secrets = secret_field_names(type(model))
    for name in secrets:
        if name in raw:
            value = getattr(model, name)
            if isinstance(value, SecretStr):
                raw[name] = value.get_secret_value()
    # Coerce non-primitive values (HttpUrl, Path, etc.) to JSON-safe strings.
    for key, value in list(raw.items()):
        raw[key] = _json_safe(value)
    return raw


def _json_safe(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return str(value)


def mask_secrets(model: BaseModel) -> dict[str, Any]:
    """JSON-safe dump with SecretStr fields replaced by a placeholder.

    ``BaseModel.model_dump(mode="json")`` already renders SecretStr as the
    constant ``"**********"``; this helper normalises to SECRET_PLACEHOLDER so
    the API contract is stable regardless of Pydantic's render choices.
    """
    data = model.model_dump(mode="json")
    for name in secret_field_names(type(model)):
        if name in data:
            data[name] = SECRET_PLACEHOLDER
    return data


def schema_with_secret_flags(cls: type[BaseModel]) -> dict[str, Any]:
    """Return ``model_json_schema()`` enriched with per-field ``secret: true``.

    Lets the UI render SecretStr fields as password inputs without having to
    guess from JSON Schema alone (which doesn't express SecretStr natively).
    """
    schema = cls.model_json_schema()
    secrets = secret_field_names(cls)
    props = schema.get("properties", {})
    for name in secrets:
        if name in props:
            props[name]["secret"] = True
    return schema


def merge_and_validate(
    cls: type[BaseModel],
    stored: dict[str, Any],
    patch: dict[str, Any],
) -> BaseModel:
    """Merge a partial patch over stored values and revalidate.

    Empty-string values for SecretStr fields mean "leave unchanged" — the
    stored secret is preserved. Absent keys in the patch also mean
    "leave unchanged". Non-secret string fields treat empty string literally.
    """
    merged: dict[str, Any] = dict(stored)
    secrets = secret_field_names(cls)
    for key, value in patch.items():
        if key in secrets and isinstance(value, str) and value == "":
            continue
        merged[key] = value
    return cls(**merged)
