import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest
from click.testing import CliRunner

from vew_cli.main import cli

API_URL = "https://api.example.test"
PROJECT_ID = "proj-1"


@dataclass
class FakeApi:
    """Serves queued responses per (method, path) and records every request."""

    routes: dict[tuple[str, str], list] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, method: str, path: str, *responses) -> None:
        self.routes.setdefault((method, path), []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(599, json={"message": f"unexpected {request.method} {request.url.path}"})
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Callable):
            response = response(request)
        status, body, *headers = response
        return httpx.Response(status, json=body, headers=headers[0] if headers else None)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for name in [
        "VEW_PROFILE",
        "VEW_API_URL",
        "VEW_COGNITO_DOMAIN",
        "VEW_CLIENT_ID",
        "VEW_IDENTITY_PROVIDER",
        "VEW_CA_BUNDLE",
        "VEW_PROJECT_ID",
        "VEW_ACCESS_TOKEN",
        "VEW_OUTPUT",
        "VEW_PACKAGING_URL",
        "VEW_PROVISIONING_URL",
        "VEW_PROJECTS_URL",
        "VEW_PUBLISHING_URL",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VEW_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture
def static_token(monkeypatch):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "static-token")
    monkeypatch.setenv("VEW_PROJECT_ID", PROJECT_ID)


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def run(api, sleeps):
    def invoke(*args, input=None, **extra):
        obj = {"transport": httpx.MockTransport(api.handler), "sleep": sleeps.append, **extra}
        return CliRunner().invoke(cli, list(args), obj=obj, input=input, catch_exceptions=False)

    return invoke


def packaging_path(*parts: str) -> str:
    return "/".join(["/packaging/projects", PROJECT_ID, *parts])


def provisioning_path(*parts: str) -> str:
    return "/".join(["/provisioning/projects", PROJECT_ID, "products", *parts])


def publishing_path(*parts: str) -> str:
    return "/".join(["/publishing/projects", PROJECT_ID, *parts])
