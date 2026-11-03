"""Entry point for the `vew` command."""

import json
import time
import webbrowser

import click
import httpx

from vew_cli import __version__, output
from vew_cli.auth import AuthError, loopback_receiver
from vew_cli.commands.packaging import packaging
from vew_cli.commands.projects import projects
from vew_cli.commands.provisioning import provisioning
from vew_cli.commands.publishing import publishing
from vew_cli.config import ConfigError, config_path
from vew_cli.context import State, pass_state
from vew_cli.http import ApiError
from vew_cli.waiting import CreateUnresolved, WaitFailed, WaitTimeout

EXIT_API_ERROR = 1
EXIT_CONFIG_ERROR = 2


class VewGroup(click.Group):
    """Reports expected failures as one-line errors with distinct exit codes instead of tracebacks."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ApiError as error:
            click.echo(f"error: {error}", err=True)
            state = ctx.find_object(State)
            if state and state.verbose and error.body:
                click.echo(json.dumps(error.body, indent=2) if isinstance(error.body, dict) else error.body, err=True)
            ctx.exit(EXIT_API_ERROR)
        except (ConfigError, AuthError) as error:
            click.echo(f"error: {error}", err=True)
            ctx.exit(EXIT_CONFIG_ERROR if isinstance(error, ConfigError) else EXIT_API_ERROR)
        except (WaitFailed, WaitTimeout, CreateUnresolved) as error:
            click.echo(f"error: {error}", err=True)
            ctx.exit(error.exit_code)
        except httpx.TransportError as error:
            click.echo(f"error: request failed: {error}", err=True)
            ctx.exit(EXIT_API_ERROR)


@click.group(cls=VewGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--profile", envvar="VEW_PROFILE", help="Config profile to use (default: 'default').")
@click.option("--project", "-p", "project_id", help="Project ID for project-scoped commands [env: VEW_PROJECT_ID].")
@click.option(
    "--output", "-o", type=click.Choice(output.FORMATS), default="json", show_default=True, envvar="VEW_OUTPUT"
)
@click.option("--verbose", "-v", is_flag=True, help="Print full error bodies.")
@click.version_option(__version__, prog_name="vew")
@click.pass_context
def cli(ctx, profile, project_id, output, verbose):
    """Command-line client for the Virtual Engineering Workbench, acting as the signed-in user.

    Run `vew login` once to sign in through the browser. Settings come from a profile in
    ~/.config/vew/config.toml, overridden by VEW_* environment variables, overridden by flags.
    Run `vew config path` for the config file location.
    """
    existing = ctx.obj if isinstance(ctx.obj, dict) else {}
    ctx.obj = State(
        profile_name=profile,
        overrides={"project_id": project_id},
        output=output,
        transport=existing.get("transport"),
        sleep=existing.get("sleep", time.sleep),
        login_receiver=existing.get("login_receiver"),
        interactive=existing.get("interactive", False),
        verbose=verbose,
    )


@cli.command()
@click.option("--no-browser", is_flag=True, help="Only print the login URL instead of opening a browser.")
@pass_state
def login(state: State, no_browser):
    """Sign in to VEW through the browser.

    The browser returns to http://localhost:8765/callback, so run this on the machine whose browser you sign
    in with. The session lasts as long as VEW's refresh token for the CLI.
    """

    def open_url(url: str) -> None:
        click.echo(f"Sign in to VEW at:\n\n  {url}\n", err=True)
        if not no_browser:
            webbrowser.open(url)

    username = state.tokens.login(state.login_receiver or loopback_receiver(open_url))
    click.echo(f"Logged in as {username}.", err=True)


@cli.command()
@pass_state
def logout(state: State):
    """Sign out: revoke the session and delete its cached tokens."""
    if state.tokens.logout():
        click.echo("Logged out.", err=True)
    else:
        click.echo("Not logged in.", err=True)


@cli.group()
def auth():
    """Inspect the signed-in session."""


@auth.command("token")
@pass_state
def print_token(state: State):
    """Print an access token, e.g. for curl: -H "Authorization: Bearer $(vew auth token)"."""
    click.echo(state.tokens.token())


@auth.command("whoami")
@pass_state
def whoami(state: State):
    """Show the signed-in user as VEW sees them (Cognito userInfo)."""
    state.emit(state.tokens.user_info())


@cli.group()
def config():
    """Inspect configuration."""


@config.command("path")
def show_path():
    """Print the config file location."""
    click.echo(config_path())


@config.command("show")
@pass_state
def show_config(state: State):
    """Show the resolved profile, with secrets redacted."""
    state.emit(state.profile.redacted())


cli.add_command(packaging)
cli.add_command(provisioning)
cli.add_command(publishing)
cli.add_command(projects)


if __name__ == "__main__":
    cli()
