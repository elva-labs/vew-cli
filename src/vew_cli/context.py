"""Per-invocation state and option helpers shared by the command groups."""

import functools
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import click
import httpx
import yaml

from vew_cli import output
from vew_cli.auth import CallbackReceiver, TokenProvider
from vew_cli.config import Profile, load_profile
from vew_cli.http import VewClient
from vew_cli.waiting import WaitSpec, find_created, wait_for


@dataclass
class State:
    profile_name: str | None = None
    overrides: dict = field(default_factory=dict)
    output: str = "json"
    verbose: bool = False
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    login_receiver: CallbackReceiver | None = None
    # Lets tests answer prompts that otherwise need a terminal.
    interactive: bool = False
    _profile: Profile | None = None
    _tokens: TokenProvider | None = None
    _clients: dict[str, VewClient] = field(default_factory=dict)

    @property
    def profile(self) -> Profile:
        if self._profile is None:
            self._profile = load_profile(self.profile_name, self.overrides)
        return self._profile

    @property
    def tokens(self) -> TokenProvider:
        if self._tokens is None:
            self._tokens = TokenProvider(self.profile, transport=self.transport)
        return self._tokens

    def client(self, api: str) -> VewClient:
        if api not in self._clients:
            self._clients[api] = VewClient(
                self.profile.api_base(api),
                self.tokens,
                transport=self.transport,
                sleep=self.sleep,
                on_retry=lambda message: click.echo(f"warning: {message}", err=True),
                verify=self.profile.tls_verify(),
            )
        return self._clients[api]

    def project_id(self) -> str:
        if not self.profile.project_id:
            raise click.UsageError(
                "No project selected. Pass --project, set VEW_PROJECT_ID, or set project_id in the profile."
            )
        return self.profile.project_id

    def emit(self, data, rows_key: str | None = None, columns: list[str] | None = None) -> None:
        output.emit(data, self.output, rows_key, columns)

    def wait(
        self, fetch: Callable[[], dict], status_of: Callable[[dict], str | None], spec: WaitSpec, label: str, opts: dict
    ) -> dict:
        return wait_for(
            fetch, status_of, spec, timeout=opts["timeout"], interval=opts["interval"], label=label, sleep=self.sleep
        )

    def find_created(
        self, list_items: Callable[[], list[dict]], id_key: str, before: set[str], matches, label: str, hint: str
    ) -> dict:
        return find_created(list_items, id_key, before, matches, label=label, hint=hint, sleep=self.sleep)


pass_state = click.make_pass_decorator(State)


def load_document(path: str) -> dict:
    """Reads a JSON or YAML request body from a file, or from stdin when the path is '-'."""
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
    except OSError as error:
        raise click.BadParameter(str(error), param_hint="--file") from error
    try:
        document = yaml.safe_load(text)  # YAML is a superset of JSON
    except yaml.YAMLError as error:
        raise click.BadParameter(f"{path} is not valid JSON or YAML: {error}", param_hint="--file") from error
    if not isinstance(document, dict):
        raise click.BadParameter(f"{path} must contain a JSON/YAML object", param_hint="--file")
    return document


def build_body(file: str | None, **fields) -> dict:
    """Starts from the --file document, if any, and overlays the flags that were given."""
    body = load_document(file) if file else {}
    for key, value in fields.items():
        if value is None or value == ():
            continue
        body[key] = list(value) if isinstance(value, tuple) else value
    return body


def require(body: dict, *keys: str) -> dict:
    missing = [key for key in keys if body.get(key) in (None, "", [])]
    if missing:
        raise click.UsageError(
            f"Missing required field(s): {', '.join(missing)}. Pass the flags or include them in --file."
        )
    return body


def parse_key_values(values: tuple[str, ...], option: str) -> list[dict]:
    pairs = []
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise click.BadParameter(f"expected KEY=VALUE, got {item!r}", param_hint=option)
        pairs.append({"key": key, "value": value})
    return pairs


def file_option(command):
    return click.option(
        "--file",
        "-f",
        "file",
        metavar="PATH",
        help="JSON or YAML request body ('-' for stdin). Flags override fields from the file.",
    )(command)


def wait_options(default_timeout: int, default_interval: int = 5):
    def decorator(command):
        @click.option("--wait/--no-wait", default=False, help="Poll until the operation reaches a terminal status.")
        @click.option("--timeout", type=int, default=default_timeout, show_default=True, help="Seconds to wait.")
        @click.option(
            "--interval", type=int, default=default_interval, show_default=True, help="Seconds between polls."
        )
        @functools.wraps(command)
        def wrapper(*args, wait, timeout, interval, **kwargs):
            return command(*args, wait_opts={"wait": wait, "timeout": timeout, "interval": interval}, **kwargs)

        return wrapper

    return decorator


def confirm_option(command):
    return click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")(command)


def confirm(yes: bool, message: str) -> None:
    if not yes:
        click.confirm(message, abort=True, err=True)
