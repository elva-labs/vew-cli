"""HTTP client for the user APIs: bearer auth, error bodies, and retries that are safe to replay."""

import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from vew_cli import __version__
from vew_cli.auth import TokenProvider

RETRYABLE_STATUSES = {429, 502, 503, 504}
MAX_BACKOFF_SECONDS = 30


@dataclass
class ApiError(Exception):
    status: int
    method: str
    url: str
    detail: str | None = None
    body: dict | str | None = field(default=None, repr=False)

    def __str__(self) -> str:
        label = f"HTTP {self.status}"
        return f"{label}: {self.detail}" if self.detail else label

    @classmethod
    def from_response(cls, response: httpx.Response) -> "ApiError":
        try:
            body = response.json()
        except ValueError:
            body = response.text or None
        error = cls(
            status=response.status_code, method=response.request.method, url=str(response.request.url), body=body
        )
        if isinstance(body, dict):
            # The handlers answer {"message": ...}; API Gateway's request validation adds "validationError", and
            # the handlers' own validation answers {"detail": [...]}.
            message, validation_error = body.get("message"), body.get("validationError")
            # API Gateway may repeat part of the message in validationError, e.g. "Missing ... [pageSize]".
            parts = [message, None if validation_error and validation_error in (message or "") else validation_error]
            if isinstance(body.get("detail"), list):
                parts.append("; ".join(_validation_message(item) for item in body["detail"]))
            error.detail = ": ".join(part for part in parts if part) or None
        elif isinstance(body, str):
            error.detail = body.strip()[:500]
        return error


class VewClient:
    def __init__(
        self,
        base_url: str,
        tokens: TokenProvider,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[str], None] | None = None,
        verify: ssl.SSLContext | bool = True,
    ):
        self._tokens = tokens
        self._max_retries = max_retries
        self._sleep = sleep
        self._on_retry = on_retry or (lambda message: None)
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            verify=verify,
            timeout=60,
            headers={"Accept": "application/json", "User-Agent": f"vew-cli/{__version__}"},
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params=params)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        # Reads are safe to repeat. The user APIs do not deduplicate writes, so a failed write surfaces the error
        # and the caller inspects state before trying again.
        replay_safe = method == "GET"
        headers = {}
        params = {k: v for k, v in (params or {}).items() if v is not None and v != []}
        refreshed_token = False
        attempt = 0

        while True:
            attempt += 1
            headers["Authorization"] = f"Bearer {self._tokens.token()}"
            try:
                response = self._http.request(method, path, json=json, params=params, headers=headers)
            except httpx.TransportError as error:
                if replay_safe and attempt <= self._max_retries:
                    self._backoff(attempt, None, f"{type(error).__name__}: {error}")
                    continue
                raise

            if response.status_code == 401 and not refreshed_token and self._tokens.invalidate():
                refreshed_token = True
                continue
            if response.is_success:
                return response.json() if response.content else {}

            error = ApiError.from_response(response)
            if replay_safe and attempt <= self._max_retries and error.status in RETRYABLE_STATUSES:
                self._backoff(attempt, response.headers.get("Retry-After"), str(error))
                continue
            raise error

    def _backoff(self, attempt: int, retry_after: str | None, reason: str) -> None:
        delay = min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        if retry_after and retry_after.isdigit():
            delay = min(int(retry_after), MAX_BACKOFF_SECONDS)
        self._on_retry(f"{reason}; retrying in {delay}s (attempt {attempt + 1}/{self._max_retries + 1})")
        self._sleep(delay)


def _validation_message(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    location = ".".join(str(part) for part in item.get("loc", []) if part != "body")
    return f"{location}: {item.get('msg')}" if location else str(item.get("msg"))
