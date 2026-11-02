# vew — VEW command-line client

`vew` is a command-line client for the Virtual Engineering Workbench. It signs in as you, through VEW's Cognito
login in the browser, and calls the same APIs as the web UI, so it can do what your project roles allow there:

| Command group | API | Base path |
| --- | --- | --- |
| `vew packaging` | Packaging: components, recipes, pipelines, image builds | `/packaging` |
| `vew provisioning` | Provisioning: product catalogue and your provisioned products | `/provisioning` |
| `vew projects` | Projects: projects, your role assignments and enrolments | `/projects` |

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd vew-cli
uv sync
uv run vew --help

# or install it on your PATH
uv tool install .
```

## Configure

Settings are resolved in this order: command-line flags, then `VEW_*` environment variables, then the profile in
`~/.config/vew/config.toml` (override the location with `VEW_CONFIG`).

```toml
# ~/.config/vew/config.toml
[default]
api_url = "https://api.example.com"                          # custom domain hosting VEW's APIs
cognito_domain = "https://vew.auth.eu-central-1.amazoncognito.com"   # VEW user pool domain
client_id = "<VEW's app client id for the CLI>"
project_id = "proj-example"
# identity_provider = "MyIdP"                                # skip Cognito's provider picker
# ca_bundle = "~/.config/vew/vew-internal.pem"               # trust a private/self-signed API certificate
```

The client id is VEW's `cli` app client, which VEW creates from `ADDITIONAL_APP_CLIENTS` in its deployment config:

```json
{"Name": "cli", "CallbackUrls": ["http://localhost:8765/callback"], "RefreshTokenValidityHours": 720}
```

The deploy summary lists the client id. The client must have no secret, and its callback must be exactly
`http://localhost:8765/callback`, the address `vew login` listens on.

Each API's URL can be overridden individually, for example when calling an `execute-api` endpoint directly:
`packaging_url`, `provisioning_url`, and `projects_url` in the profile, or `VEW_PACKAGING_URL`,
`VEW_PROVISIONING_URL`, and `VEW_PROJECTS_URL`.

| Environment variable | Setting |
| --- | --- |
| `VEW_PROFILE` | Profile name (`--profile`) |
| `VEW_API_URL` | `api_url` |
| `VEW_COGNITO_DOMAIN` | `cognito_domain` |
| `VEW_CLIENT_ID` | `client_id` |
| `VEW_IDENTITY_PROVIDER` | `identity_provider` |
| `VEW_CA_BUNDLE` | `ca_bundle`: PEM certificates to trust for the APIs (not for Cognito) |
| `VEW_PROJECT_ID` | `project_id` (`--project`/`-p`) |
| `VEW_ACCESS_TOKEN` | Use this bearer token as-is instead of the signed-in session |
| `VEW_OUTPUT` | Output format (`--output`/`-o`) |

## Sign in

```bash
vew login            # opens the browser; --no-browser only prints the URL
vew auth whoami      # the user VEW sees
vew logout           # revokes the session
```

Then run `vew projects use` to choose the project the other commands work in.

`vew login` uses the authorization code grant with PKCE and waits for the browser on `localhost:8765`, so sign in
with a browser on the same machine. The session is cached in
`~/.cache/vew/tokens.json` (mode `0600`); the CLI refreshes the hour-long access token itself until the refresh
token expires, then asks you to log in again. `vew auth token` prints an access token for other tools.

## Usage

```bash
vew -o table packaging components list
vew packaging components create --name build-agent --description "Installs the build agent" \
  --platform Linux --arch amd64 --os-version "Ubuntu 24"

# Structured component definitions are easiest to keep in a file (JSON or YAML)
vew packaging component-versions create comp-123 --release-type MAJOR \
  --description "Install the build agent" --definition build-agent.yaml \
  --software-vendor "Example Corp" --software-version 1.0.0 --wait
vew packaging component-versions release comp-123 vers-456

# Any request body can come from --file (or stdin with -f -); flags override fields from the file
vew packaging recipe-versions create reci-123 -f recipe-version.yaml --wait
vew packaging pipelines create -f pipeline.json --wait
vew packaging images build --pipeline-id pipe-123 --wait
vew packaging images get imag-123 -o table      # imageUpstreamId is the AMI ID

vew -o table provisioning products list --type WORKBENCH
vew provisioning products versions prod-123 --stage PROD --region eu-west-1
vew provisioning provisioned launch --product-id prod-123 --version-id vers-456 \
  --stage PROD --region eu-west-1 --param InstanceType=m8i.2xlarge --wait
vew -o table provisioning provisioned list --status RUNNING
vew -o table provisioning provisioned list --project-wide --all    # PROGRAM_OWNER or ADMIN
vew provisioning provisioned stop pp-123 --wait
vew provisioning provisioned remove pp-123 --yes

vew -o table projects list
vew projects use                 # pick one of your projects; saved as project_id in the profile
vew projects use proj-123        # or name it directly
```

Run `vew <group> <command> --help` for every option.

### Permissions

The CLI can do what your role in the project allows in the web UI. Packaging needs at least PRODUCT_CONTRIBUTOR,
and retiring a released component or recipe version needs ADMIN, PROGRAM_OWNER, or POWER_USER. Launch, start,
stop, and remove act on your own provisioned products; `provisioned list --project-wide` needs PROGRAM_OWNER or
ADMIN.

### Creates

VEW's APIs accept creates (component versions, recipes, recipe versions, pipelines, image builds, launches) without
returning the new resource's ID. The CLI lists the resources just before the create and finds the one that appeared
afterwards with the fields you sent, such as the pipeline name, then prints its ID. If two identical creates run at
the same time, it cannot tell them apart and exits with code 5 rather than guessing.

Creates are not idempotent: if a create fails or exits with 5, check the list before running it again. Only GET
requests are retried automatically, after network errors and `429`/`5xx` responses, honouring `Retry-After`.

### Waiting

Asynchronous operations return as soon as the resource ID is known. Add `--wait` to poll until the resource reaches a
terminal status; `--timeout` and `--interval` tune the polling. Progress is printed to stderr and the final
resource to stdout.

### Output and exit codes

`--output`/`-o` selects `json` (default), `yaml`, or `table`. Data goes to stdout; prompts, progress, and errors go to
stderr, so `vew ... | jq` works.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | API or authentication error (`-v` prints the full error body) |
| 2 | Usage or configuration error |
| 3 | `--wait` ended in a failure status, such as `FAILED` |
| 4 | `--wait` timed out |
| 5 | A create was accepted but the new resource could not be identified |

Archive, retire, revoke, and remove commands prompt for confirmation; pass `--yes` in scripts.

## Develop

```bash
cd vew-cli
uv sync
uv run pytest
```
