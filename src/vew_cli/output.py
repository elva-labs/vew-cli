"""Rendering of API responses as JSON, YAML, or a plain-text table."""

import json

import click
import yaml

FORMATS = ["json", "yaml", "table"]


def emit(data, fmt: str, rows_key: str | None = None, columns: list[str] | None = None) -> None:
    if fmt == "json":
        click.echo(json.dumps(data, indent=2, default=str))
    elif fmt == "yaml":
        click.echo(yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip())
    elif rows_key is not None and isinstance(data.get(rows_key), list):
        click.echo(table(data[rows_key], columns))
    else:
        click.echo(record(_unwrap(data)))


def table(rows: list[dict], columns: list[str] | None = None) -> str:
    if not rows:
        return "(none)"
    columns = columns or list(rows[0])
    cells = [[_cell(row.get(column)) for column in columns] for row in rows]
    widths = [max(len(column), *(len(line[i]) for line in cells)) for i, column in enumerate(columns)]
    lines = ["  ".join(column.ljust(width) for column, width in zip(columns, widths)).rstrip()]
    lines += ["  ".join(value.ljust(width) for value, width in zip(line, widths)).rstrip() for line in cells]
    return "\n".join(lines)


def record(data: dict) -> str:
    if not data:
        return "(empty response)"
    width = max(len(key) for key in data)
    return "\n".join(f"{key.ljust(width)}  {_cell(value, limit=None)}" for key, value in data.items())


def _unwrap(data: dict) -> dict:
    # Single-resource responses wrap the resource, e.g. {"component": {...}}; show the resource fields.
    if len(data) == 1 and isinstance(next(iter(data.values())), dict):
        return next(iter(data.values()))
    return data


def _cell(value, limit: int | None = 60) -> str:
    if value is None:
        return "-"
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        text = ",".join(str(item) for item in value)
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    if limit and len(text) > limit:
        return text[: limit - 1] + "…"
    return text
