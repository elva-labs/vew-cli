"""Profile resolution: command-line flags override environment variables, which override the config file."""

import json
import os
import re
import ssl
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# API Gateway base-path mappings of the user APIs on the custom domain (backend/infra/constants.py).
DEFAULT_API_PATHS = {
    "packaging": "packaging",
    "provisioning": "provisioning",
    "publishing": "publishing",
    "projects": "projects",
}

ENV_SETTINGS = {
    "api_url": "VEW_API_URL",
    "cognito_domain": "VEW_COGNITO_DOMAIN",
    "client_id": "VEW_CLIENT_ID",
    "identity_provider": "VEW_IDENTITY_PROVIDER",
    "ca_bundle": "VEW_CA_BUNDLE",
    "project_id": "VEW_PROJECT_ID",
    "access_token": "VEW_ACCESS_TOKEN",
}

ENV_API_URLS = {api: f"VEW_{api.upper()}_URL" for api in DEFAULT_API_PATHS}


class ConfigError(Exception):
    pass


def config_path() -> Path:
    if explicit := os.environ.get("VEW_CONFIG"):
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "vew" / "config.toml"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "vew"


@dataclass(frozen=True)
class Profile:
    name: str
    api_url: str | None = None
    cognito_domain: str | None = None
    client_id: str | None = None
    identity_provider: str | None = None
    # Certificates to trust for VEW's APIs, for a deployment whose custom domain uses a private or self-signed one.
    ca_bundle: str | None = None
    project_id: str | None = None
    access_token: str | None = None
    api_urls: dict[str, str] = field(default_factory=dict)

    def api_base(self, api: str) -> str:
        if url := self.api_urls.get(api):
            return url.rstrip("/")
        if not self.api_url:
            raise ConfigError(
                f"No API URL configured. Set api_url in the profile, VEW_API_URL, "
                f"or {ENV_API_URLS[api]} for the {api} API."
            )
        return f"{self.api_url.rstrip('/')}/{DEFAULT_API_PATHS[api]}"

    def tls_verify(self) -> ssl.SSLContext | bool:
        if not self.ca_bundle:
            return True
        path = Path(self.ca_bundle).expanduser()
        try:
            return ssl.create_default_context(cafile=path)
        except (OSError, ssl.SSLError) as error:
            raise ConfigError(f"Cannot load ca_bundle {path}: {error}") from error

    def redacted(self) -> dict:
        values = {
            "profile": self.name,
            "api_url": self.api_url,
            "cognito_domain": self.cognito_domain,
            "client_id": self.client_id,
            "identity_provider": self.identity_provider,
            "ca_bundle": self.ca_bundle,
            "project_id": self.project_id,
            "access_token": "<set>" if self.access_token else None,
        }
        for api in DEFAULT_API_PATHS:
            try:
                values[f"{api}_url"] = self.api_base(api)
            except ConfigError:
                values[f"{api}_url"] = None
        return values


def load_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid config file {path}: {error}") from error


def load_profile(name: str | None = None, overrides: dict | None = None) -> Profile:
    path = config_path()
    document = load_file(path)
    explicit_name = name or os.environ.get("VEW_PROFILE")
    profile_name = explicit_name or "default"
    if explicit_name and profile_name not in document:
        raise ConfigError(f"Profile '{profile_name}' not found in {path}.")

    values: dict = dict(document.get(profile_name, {}))
    api_urls = {api: values.pop(f"{api}_url") for api in DEFAULT_API_PATHS if f"{api}_url" in values}
    values.update(_set_values(os.environ, ENV_SETTINGS))
    api_urls.update(_set_values(os.environ, ENV_API_URLS))
    values.update({key: value for key, value in (overrides or {}).items() if value is not None})

    known = {f for f in Profile.__dataclass_fields__ if f not in {"name", "api_urls"}}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"Unknown setting(s) in profile '{profile_name}': {', '.join(sorted(unknown))}")
    return Profile(name=profile_name, api_urls=api_urls, **values)


def save_setting(profile_name: str, key: str, value: str) -> Path:
    """Sets one string setting in a profile of the config file, keeping the rest of the file as it is."""
    path = config_path()
    lines = path.read_text().splitlines() if path.exists() else []
    entry = f"{key} = {json.dumps(value)}"  # a JSON string is also a valid TOML basic string

    header = next((i for i, line in enumerate(lines) if line.strip() == f"[{profile_name}]"), None)
    if header is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[{profile_name}]", entry]
    else:
        end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        section = range(header + 1, end)
        active = [i for i in section if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])]
        commented = [i for i in section if re.match(rf"\s*#\s*{re.escape(key)}\s*=", lines[i])]
        if active or commented:
            lines[(active or commented)[0]] = entry
        else:
            last = max((i for i in section if lines[i].strip()), default=header)
            lines.insert(last + 1, entry)

    text = "\n".join(lines) + "\n"
    try:
        saved = tomllib.loads(text)[profile_name][key]
    except (tomllib.TOMLDecodeError, KeyError) as error:
        raise ConfigError(f"Could not set {key} in {path}; edit it by hand ({error}).") from error
    if saved != value:
        raise ConfigError(f"Could not set {key} in {path}; edit it by hand.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _set_values(environ, names: dict[str, str]) -> dict[str, str]:
    return {key: environ[env_name] for key, env_name in names.items() if environ.get(env_name)}
