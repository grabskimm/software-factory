"""Minimal JSON Schema checker for the keyword subset factory/schemas.py uses."""

import re


def errors(value, schema, path="$"):
    found = []
    if "anyOf" in schema:
        if any(not errors(value, option, path) for option in schema["anyOf"]):
            return []
        return [f"{path}: matches no anyOf option"]
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    }
    if kind and not checks[kind](value):
        return [f"{path}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: not in enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            found.append(f"{path}: too short")
        if len(value) > schema.get("maxLength", len(value)):
            found.append(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(f"{path}: pattern mismatch")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            found.append(f"{path}: below minimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            found.append(f"{path}: too few items")
        if len(value) > schema.get("maxItems", len(value)):
            found.append(f"{path}: too many items")
        for index, item in enumerate(value):
            found += errors(item, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                found.append(f"{path}: missing {name}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in schema.get("properties", {}):
                    found.append(f"{path}: unexpected {name}")
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                found += errors(value[name], sub, f"{path}.{name}")
    return found
