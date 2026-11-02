"""Signed-in user tokens: browser login with the authorization code grant and PKCE, refreshed and cached on disk."""

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import httpx

from vew_cli.config import ConfigError, Profile, cache_dir

# VEW's Cognito app client for the CLI allows exactly this callback (ADDITIONAL_APP_CLIENTS in VEW's config).
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
# VEW's API authorizer resolves the caller through Cognito's userInfo endpoint, which needs these scopes.
SCOPES = "openid email profile"
EXPIRY_MARGIN_SECONDS = 60
LOGIN_TIMEOUT_SECONDS = 300

# Receives the browser's redirect: given the authorize URL, returns the callback's query parameters.
CallbackReceiver = Callable[[str], dict[str, str]]


class AuthError(Exception):
    pass


class TokenProvider:
    def __init__(self, profile: Profile, transport: httpx.BaseTransport | None = None, cache: Path | None = None):
        self._profile = profile
        self._transport = transport
        self._cache_file = (cache or cache_dir()) / "tokens.json"
        self._token: str | None = None

    def token(self) -> str:
        if self._profile.access_token:
            return self._profile.access_token
        if self._token:
            return self._token

        key = self._cache_key()
        entry = self._read_cache().get(key)
        if not entry:
            raise AuthError("Not logged in. Run `vew login`.")
        if entry["expires_at"] - EXPIRY_MARGIN_SECONDS <= time.time():
            entry = self._refresh(key, entry)
        self._token = entry["access_token"]
        return self._token

    def invalidate(self) -> bool:
        """Expires the cached access token. Returns False when there is no refresh token to replace it with."""
        if self._profile.access_token:
            return False
        self._token = None
        cached = self._read_cache()
        entry = cached.get(self._cache_key())
        if not entry or not entry.get("refresh_token"):
            return False
        entry["expires_at"] = 0
        self._save(cached)
        return True

    def login(self, receive: CallbackReceiver) -> str:
        """Signs the user in through the browser and caches their tokens. Returns the Cognito username."""
        domain, client_id = self._endpoint()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self._profile.identity_provider:
            query["identity_provider"] = self._profile.identity_provider

        callback = receive(f"{domain}/oauth2/authorize?{urllib.parse.urlencode(query)}")
        if callback.get("state") != state:
            raise AuthError("Login failed: the callback's state does not match this login.")
        if "error" in callback:
            raise AuthError(f"Login failed: {callback['error']} {callback.get('error_description', '')}".rstrip())
        if not callback.get("code"):
            raise AuthError("Login failed: the callback carried no authorization code.")

        body = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": callback["code"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        )
        entry = self._entry(body, refresh_token=body.get("refresh_token"))
        cached = self._read_cache()
        cached[self._cache_key()] = entry
        self._save(cached)
        self._token = entry["access_token"]
        return entry["username"]

    def logout(self) -> bool:
        """Revokes the refresh token and forgets the session. Returns False when nobody was logged in."""
        cached = self._read_cache()
        entry = cached.pop(self._cache_key(), None)
        if entry is None:
            return False
        self._save(cached)
        self._token = None
        if entry.get("refresh_token"):
            domain, client_id = self._endpoint()
            # Best effort: the local session is gone either way.
            with httpx.Client(transport=self._transport, timeout=30) as client:
                try:
                    client.post(
                        f"{domain}/oauth2/revoke", data={"token": entry["refresh_token"], "client_id": client_id}
                    )
                except httpx.TransportError:
                    pass
        return True

    def user_info(self) -> dict:
        domain, _ = self._endpoint()
        with httpx.Client(transport=self._transport, timeout=30) as client:
            try:
                response = client.get(f"{domain}/oauth2/userInfo", headers={"Authorization": f"Bearer {self.token()}"})
            except httpx.TransportError as error:
                raise AuthError(f"userInfo request failed: {error}") from error
        if response.status_code != 200:
            raise AuthError(f"userInfo request failed with HTTP {response.status_code}: {response.text.strip()}")
        return response.json()

    def _refresh(self, key: str, entry: dict) -> dict:
        _, client_id = self._endpoint()
        try:
            body = self._token_request(
                {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": entry["refresh_token"]}
            )
        except AuthError:
            cached = self._read_cache()
            cached.pop(key, None)
            self._save(cached)
            raise AuthError("Your VEW session has expired. Run `vew login`.") from None
        # Cognito does not rotate refresh tokens, so the response carries only new access and ID tokens.
        refreshed = self._entry(body, refresh_token=body.get("refresh_token") or entry["refresh_token"])
        cached = self._read_cache()
        cached[key] = refreshed
        self._save(cached)
        return refreshed

    @staticmethod
    def _entry(body: dict, refresh_token: str | None) -> dict:
        return {
            "access_token": body["access_token"],
            "refresh_token": refresh_token,
            "expires_at": time.time() + int(body.get("expires_in", 3600)),
            "username": _claims(body["access_token"]).get("username"),
        }

    def _token_request(self, form: dict) -> dict:
        domain, _ = self._endpoint()
        with httpx.Client(transport=self._transport, timeout=30) as client:
            try:
                response = client.post(f"{domain}/oauth2/token", data=form)
            except httpx.TransportError as error:
                raise AuthError(f"Token request failed: {error}") from error
        if response.status_code != 200:
            raise AuthError(f"Token request failed with HTTP {response.status_code}: {response.text.strip()}")
        return response.json()

    def _endpoint(self) -> tuple[str, str]:
        profile = self._profile
        if not profile.cognito_domain or not profile.client_id:
            raise ConfigError(
                "No login configured. Set cognito_domain and client_id in the profile, "
                "or VEW_COGNITO_DOMAIN and VEW_CLIENT_ID, or provide VEW_ACCESS_TOKEN."
            )
        return profile.cognito_domain.rstrip("/"), profile.client_id

    def _cache_key(self) -> str:
        identity = "|".join([self._profile.cognito_domain or "", self._profile.client_id or ""])
        return hashlib.sha256(identity.encode()).hexdigest()

    def _read_cache(self) -> dict:
        try:
            return json.loads(self._cache_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, cached: dict) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._cache_file.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(cached, handle)
        temporary.replace(self._cache_file)


def _claims(token: str) -> dict:
    """Reads a JWT's payload without verifying it; only for display, VEW verifies the token itself."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class _CallbackServer(http.server.HTTPServer):
    received: dict[str, str]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self):  # noqa: N802 - http.server's naming
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        self.server.received = dict(urllib.parse.parse_qsl(url.query))
        message = "Login failed, see the terminal." if "error" in self.server.received else "Logged in to VEW."
        page = f"<html><body><p>{message} You can close this tab.</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format, *args):
        pass


def loopback_receiver(open_url: Callable[[str], None], timeout: float = LOGIN_TIMEOUT_SECONDS) -> CallbackReceiver:
    """Serves REDIRECT_URI once on the loopback interface and returns the redirect's query parameters."""

    def receive(authorize_url: str) -> dict[str, str]:
        try:
            server = _CallbackServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        except OSError as error:
            raise AuthError(f"Cannot listen on port {CALLBACK_PORT} for the login callback: {error}") from error
        with server:
            server.received = {}
            server.timeout = 1
            open_url(authorize_url)
            deadline = time.monotonic() + timeout
            while not server.received:
                if time.monotonic() > deadline:
                    raise AuthError(f"Login timed out after {timeout:.0f}s waiting for the browser.")
                server.handle_request()
            return server.received

    return receive
