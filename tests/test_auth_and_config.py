import base64
import hashlib
import json
import os
import stat
import time
import urllib.parse

from tests.conftest import API_URL, PROJECT_ID

COGNITO_DOMAIN = "https://vew.auth.example.test"


def write_config(path, text):
    path.write_text(text)


def jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def configure_login(monkeypatch):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("VEW_COGNITO_DOMAIN", COGNITO_DOMAIN)
    monkeypatch.setenv("VEW_CLIENT_ID", "cli-client")


def browser(seen: list, **callback):
    """Stands in for the browser and the loopback listener: records the authorize URL and answers the redirect."""

    def receive(authorize_url: str) -> dict:
        seen.append(authorize_url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query))
        return {"state": query["state"], "code": "auth-code", **callback}

    return receive


def form(request) -> dict:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def cache_file(isolated_env):
    return isolated_env / "cache" / "vew" / "tokens.json"


def seed_session(isolated_env, access_token="cached", refresh_token="refresh-1", expires_in=3600):
    key = hashlib.sha256(f"{COGNITO_DOMAIN}|cli-client".encode()).hexdigest()
    path = cache_file(isolated_env)
    path.parent.mkdir(parents=True)
    entry = {"access_token": access_token, "refresh_token": refresh_token, "expires_at": time.time() + expires_in}
    path.write_text(json.dumps({key: entry}))


def test_login_uses_pkce_and_caches_the_session(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)
    monkeypatch.setenv("VEW_IDENTITY_PROVIDER", "Coretura")
    access = jwt({"username": "jdoe", "token_use": "access"})
    api.add("POST", "/oauth2/token", (200, {"access_token": access, "refresh_token": "refresh-1", "expires_in": 3600}))
    api.add("GET", "/packaging/projects/proj-1/components", (200, {"components": []}))
    seen = []

    result = run("login", login_receiver=browser(seen))

    assert result.exit_code == 0, result.output
    assert "Logged in as jdoe." in result.stderr
    authorize = urllib.parse.urlsplit(seen[0])
    query = dict(urllib.parse.parse_qsl(authorize.query))
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == f"{COGNITO_DOMAIN}/oauth2/authorize"
    assert query["client_id"] == "cli-client"
    assert query["redirect_uri"] == "http://localhost:8765/callback"
    assert query["scope"] == "openid email profile"
    assert query["identity_provider"] == "Coretura"
    assert query["code_challenge_method"] == "S256"

    exchange = form(api.requests[0])
    assert "Authorization" not in api.requests[0].headers
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "auth-code"
    assert exchange["client_id"] == "cli-client"
    assert exchange["redirect_uri"] == "http://localhost:8765/callback"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == query["code_challenge"]
    assert stat.S_IMODE(os.stat(cache_file(isolated_env)).st_mode) == 0o600

    assert run("packaging", "components", "list").exit_code == 0
    assert api.requests[-1].headers["Authorization"] == f"Bearer {access}"


def test_login_rejects_a_callback_for_another_login(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)

    result = run("login", login_receiver=browser([], state="forged"))

    assert result.exit_code == 1
    assert "state does not match" in result.stderr
    assert api.requests == []
    assert not cache_file(isolated_env).exists()


def test_login_reports_an_error_callback(api, run, monkeypatch):
    configure_login(monkeypatch)

    result = run("login", login_receiver=browser([], error="access_denied", error_description="User cancelled"))

    assert result.exit_code == 1
    assert "access_denied User cancelled" in result.stderr
    assert api.requests == []


def test_expired_access_token_is_refreshed_and_the_refresh_token_kept(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)
    seed_session(isolated_env, access_token="old", expires_in=10)
    api.add("POST", "/oauth2/token", (200, {"access_token": "new", "expires_in": 3600}))
    api.add("GET", "/packaging/projects/proj-1/images", (200, {"images": []}))

    assert run("packaging", "images", "list").exit_code == 0
    assert run("packaging", "images", "list").exit_code == 0

    token_requests = [r for r in api.requests if r.url.path == "/oauth2/token"]
    assert len(token_requests) == 1
    assert form(token_requests[0]) == {
        "grant_type": "refresh_token",
        "client_id": "cli-client",
        "refresh_token": "refresh-1",
    }
    assert [r.headers["Authorization"] for r in api.requests if r.method == "GET"] == ["Bearer new"] * 2
    assert list(json.loads(cache_file(isolated_env).read_text()).values())[0]["refresh_token"] == "refresh-1"


def test_unauthorized_response_refreshes_once(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)
    seed_session(isolated_env, access_token="stale")
    api.add("POST", "/oauth2/token", (200, {"access_token": "fresh", "expires_in": 3600}))
    api.add("GET", "/packaging/projects/proj-1/images", (401, {"message": "Unauthorized"}), (200, {"images": []}))

    result = run("packaging", "images", "list")

    assert result.exit_code == 0, result.output
    assert [r.headers["Authorization"] for r in api.requests if r.method == "GET"] == ["Bearer stale", "Bearer fresh"]


def test_failed_refresh_ends_the_session(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)
    seed_session(isolated_env, expires_in=0)
    api.add("POST", "/oauth2/token", (400, {"error": "invalid_grant"}))

    result = run("auth", "token")

    assert result.exit_code == 1
    assert "session has expired. Run `vew login`" in result.stderr
    assert json.loads(cache_file(isolated_env).read_text()) == {}


def test_not_logged_in(api, run, monkeypatch):
    configure_login(monkeypatch)

    result = run("packaging", "components", "list")

    assert result.exit_code == 1
    assert "Not logged in. Run `vew login`." in result.stderr
    assert api.requests == []


def test_missing_login_settings_is_a_config_error(api, run, monkeypatch):
    monkeypatch.setenv("VEW_API_URL", API_URL)

    result = run("login", login_receiver=browser([]))

    assert result.exit_code == 2
    assert "No login configured" in result.stderr


def test_logout_revokes_the_refresh_token(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)
    seed_session(isolated_env)
    api.add("POST", "/oauth2/revoke", (200, {}))

    result = run("logout")

    assert result.exit_code == 0, result.output
    assert form(api.requests[0]) == {"token": "refresh-1", "client_id": "cli-client"}
    assert json.loads(cache_file(isolated_env).read_text()) == {}
    assert "Not logged in." in run("logout").stderr


def test_whoami_shows_user_info(api, run, monkeypatch, isolated_env):
    configure_login(monkeypatch)
    seed_session(isolated_env)
    api.add("GET", "/oauth2/userInfo", (200, {"email": "jdoe@example.test", "custom:user_tid": "jdoe@example.test"}))

    result = run("auth", "whoami")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["email"] == "jdoe@example.test"
    assert api.requests[0].headers["Authorization"] == "Bearer cached"


def test_profile_file_env_and_flag_precedence(api, run, monkeypatch, isolated_env):
    write_config(
        isolated_env / "config.toml",
        f"""
[default]
api_url = "https://default.example.test"
access_token = "file-token"
project_id = "proj-file"

[other]
api_url = "{API_URL}"
access_token = "other-token"
project_id = "proj-file"
cognito_domain = "{COGNITO_DOMAIN}"
client_id = "cli-client"
projects_url = "https://projects.example.test/stage"
""",
    )
    monkeypatch.setenv("VEW_PROJECT_ID", "proj-env")
    api.add("GET", "/stage/projects/proj-flag", (200, {"project": {"projectId": "proj-flag"}}))

    result = run("--profile", "other", "-p", "proj-flag", "projects", "get")

    assert result.exit_code == 0, result.output
    assert api.requests[0].url.host == "projects.example.test"
    assert api.requests[0].headers["Authorization"] == "Bearer other-token"

    shown = json.loads(run("--profile", "other", "config", "show").stdout)
    assert shown["project_id"] == "proj-env"
    assert shown["access_token"] == "<set>"
    assert shown["cognito_domain"] == COGNITO_DOMAIN
    assert shown["packaging_url"] == f"{API_URL}/packaging"
    assert shown["projects_url"] == "https://projects.example.test/stage"


def test_unknown_profile_is_rejected(api, run, isolated_env):
    write_config(isolated_env / "config.toml", "[default]\napi_url = 'x'\n")

    result = run("--profile", "nope", "config", "show")

    assert result.exit_code == 2
    assert "Profile 'nope' not found" in result.stderr


def test_unknown_setting_is_rejected(api, run, isolated_env):
    write_config(isolated_env / "config.toml", "[default]\nclient_secret = 'x'\n")

    result = run("config", "show")

    assert result.exit_code == 2
    assert "client_secret" in result.stderr


def test_projects_list_uses_projects_base_path(api, run, monkeypatch):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add(
        "GET",
        "/projects/projects",
        (200, {"projects": [{"projectId": "proj-1", "projectName": "One"}], "nextToken": {"pk": "a"}}),
    )

    result = run("-o", "table", "projects", "list", "--page-size", "5")

    assert result.exit_code == 0, result.output
    assert api.requests[0].url.params["pageSize"] == "5"
    assert "proj-1" in result.stdout
    assert 'nextToken: {"pk": "a"}' in result.stderr


def test_unreadable_ca_bundle_is_a_config_error(api, run, monkeypatch, isolated_env):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    monkeypatch.setenv("VEW_CA_BUNDLE", str(isolated_env / "missing.pem"))

    result = run("projects", "list")

    assert result.exit_code == 2
    assert "Cannot load ca_bundle" in result.stderr


def test_projects_list_sends_the_required_page_size(api, run, monkeypatch):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add("GET", "/projects/projects", (200, {"projects": []}))

    result = run("projects", "list")

    assert result.exit_code == 0, result.output
    assert api.requests[0].url.params["pageSize"] == "100"


def test_repeated_validation_error_is_shown_once(api, run, monkeypatch):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add(
        "GET",
        "/projects/projects",
        (400, {"message": "Missing required request parameters: [pageSize]", "validationError": "[pageSize]"}),
    )

    result = run("projects", "list")

    assert result.exit_code == 1
    assert result.stderr.strip() == "error: HTTP 400: Missing required request parameters: [pageSize]"


def use_api(monkeypatch, api):
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add(
        "GET",
        "/projects/projects",
        (
            200,
            {
                "projects": [
                    {"projectId": "proj-b", "projectName": "Bravo"},
                    {"projectId": "proj-a", "projectName": "alpha"},
                    {"projectId": "proj-x", "projectName": "Not mine"},
                ],
                "assignments": [
                    {"projectId": "proj-b", "roles": ["ADMIN"]},
                    {"projectId": "proj-a", "roles": ["PLATFORM_USER", "PRODUCT_CONTRIBUTOR"]},
                ],
            },
        ),
    )


def test_projects_use_saves_the_project_and_keeps_the_rest_of_the_file(api, run, monkeypatch, isolated_env):
    config = isolated_env / "config.toml"
    write_config(
        config,
        f"""# my settings
[default]
api_url = "{API_URL}"
# project_id = "proj-..."   # pick one

[other]
project_id = "proj-other"
""",
    )
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add("GET", "/projects/projects/proj-9", (200, {"project": {"projectId": "proj-9", "projectName": "Nine"}}))

    result = run("projects", "use", "proj-9")

    assert result.exit_code == 0, result.output
    assert "Using project Nine (proj-9) in profile 'default'" in result.stderr
    assert config.read_text() == f"""# my settings
[default]
api_url = "{API_URL}"
project_id = "proj-9"

[other]
project_id = "proj-other"
"""
    assert json.loads(run("config", "show").stdout)["project_id"] == "proj-9"


def test_projects_use_adds_the_profile_when_missing(api, run, monkeypatch, isolated_env):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add("GET", "/projects/projects/proj-9", (200, {"project": {"projectId": "proj-9"}}))

    result = run("projects", "use", "proj-9")

    assert result.exit_code == 0, result.output
    assert (isolated_env / "config.toml").read_text() == '[default]\nproject_id = "proj-9"\n'


def test_projects_use_rejects_a_project_you_cannot_access(api, run, monkeypatch, isolated_env):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add("GET", "/projects/projects/proj-9", (403, {"message": "Client error"}))

    result = run("projects", "use", "proj-9")

    assert result.exit_code == 1
    assert not (isolated_env / "config.toml").exists()


def test_projects_use_picks_from_your_projects(api, run, monkeypatch, isolated_env):
    write_config(isolated_env / "config.toml", f'[default]\napi_url = "{API_URL}"\nproject_id = "proj-b"\n')
    use_api(monkeypatch, api)

    result = run("projects", "use", input="1\n", interactive=True)

    assert result.exit_code == 0, result.output
    menu = result.stderr.splitlines()
    assert menu[0].split() == ["#", "projectId", "projectName", "roles"]
    assert menu[1].split() == ["1", "proj-a", "alpha", "PLATFORM_USER,PRODUCT_CONTRIBUTOR"]
    assert menu[2].split() == ["2", "*", "proj-b", "Bravo", "ADMIN"]
    assert "Project [2]:" in result.stderr
    assert "Not mine" not in result.stderr
    assert 'project_id = "proj-a"' in (isolated_env / "config.toml").read_text()


def test_projects_use_without_a_terminal_needs_a_project_id(api, run, monkeypatch, isolated_env):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    use_api(monkeypatch, api)

    result = run("projects", "use")

    assert result.exit_code == 2
    assert "Pass a PROJECT_ID" in result.stderr


def test_projects_use_warns_when_the_environment_overrides_it(api, run, monkeypatch, isolated_env):
    monkeypatch.setenv("VEW_API_URL", API_URL)
    monkeypatch.setenv("VEW_PROJECT_ID", "proj-env")
    monkeypatch.setenv("VEW_ACCESS_TOKEN", "t")
    api.add("GET", "/projects/projects/proj-9", (200, {"project": {"projectId": "proj-9"}}))

    result = run("projects", "use", "proj-9")

    assert result.exit_code == 0, result.output
    assert "VEW_PROJECT_ID is set and overrides" in result.stderr
