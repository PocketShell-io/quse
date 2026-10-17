"""Command-line interface for quse."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import click

from quse._shared import reset_at_to_iso
from quse.usage import UnknownProviderError, collect_usage, format_usage_line


def _json_default(value: Any) -> Any:
    """Serialize non-JSON-native values. All ``datetime`` reset/expiry fields
    are emitted as canonical ISO-8601 UTC strings (the only datetime form JSON
    has), so every consumer reads one format."""
    if isinstance(value, datetime):
        return reset_at_to_iso(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_usage_payload(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for record in records:
        provider_record = dict(record)
        provider = provider_record.pop("provider")
        payload[provider] = provider_record
    return payload


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("providers", required=False, nargs=-1)
@click.option(
    "--json", "json_output", is_flag=True, help="Emit machine-readable JSON output."
)
def usage_command(
    providers: tuple[str, ...],
    json_output: bool = False,
) -> int:
    try:
        records: list[dict[str, Any]] = []
        for provider in providers:
            records.extend(collect_usage(provider))
        if not records:
            records = collect_usage(None)
    except UnknownProviderError as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        click.echo(
            json.dumps(
                _json_usage_payload(records),
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        )
        return 0

    # A single named provider keeps the compact headerless form. As soon
    # as two or more providers are listed (e.g. `quse codex codex-orin`)
    # — or all providers are listed — each record needs its provider
    # header so the rows stay attributable.
    show_header = len(providers) != 1
    for record in records:
        click.echo(format_usage_line(record, header=show_header))
    return 0


app = usage_command


def main(argv: list[str] | None = None) -> int:
    try:
        result = app.main(args=argv, prog_name="quse", standalone_mode=False)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    if result is None:
        return 0
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
