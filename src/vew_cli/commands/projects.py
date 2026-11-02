"""Projects API: the projects the signed-in user can see."""

import json
import os
import sys

import click

from vew_cli import output
from vew_cli.config import save_setting
from vew_cli.context import State, pass_state

PROJECT_COLUMNS = ["projectId", "projectName", "projectDescription"]


@click.group()
def projects():
    """Projects (Projects API)."""


@projects.command("list")
@click.option("--page-size", type=click.IntRange(1, 100), default=100, show_default=True)
@click.option("--next-token", help="nextToken from a previous page, as JSON.")
@pass_state
def list_projects(state: State, page_size, next_token):
    """List projects, with your role assignments and enrolments."""
    response = state.client("projects").get("/projects", params={"pageSize": page_size, "nextToken": next_token})
    if state.output == "table" and response.get("nextToken"):
        click.echo(f"nextToken: {json.dumps(response['nextToken'])}", err=True)
    state.emit(response, "projects", PROJECT_COLUMNS)


@projects.command("get")
@click.argument("project_id", required=False)
@pass_state
def get_project(state: State, project_id):
    """Show a project (default: the selected project)."""
    state.emit(state.client("projects").get(f"/projects/{project_id or state.project_id()}"))


@projects.command("use")
@click.argument("project_id", required=False)
@pass_state
def use_project(state: State, project_id):
    """Select the project later commands use, saved as project_id in the profile.

    Without PROJECT_ID, pick one of your projects from a list.
    """
    if project_id:
        project = state.client("projects").get(f"/projects/{project_id}")["project"]
    else:
        project = _pick_project(state)

    path = save_setting(state.profile.name, "project_id", project["projectId"])
    name = project.get("projectName") or project["projectId"]
    click.echo(f"Using project {name} ({project['projectId']}) in profile '{state.profile.name}' ({path}).", err=True)
    if os.environ.get("VEW_PROJECT_ID"):
        click.echo("warning: VEW_PROJECT_ID is set and overrides the profile's project_id.", err=True)


def _pick_project(state: State) -> dict:
    # One page is enough: the response lists only the first page of the caller's assignments either way.
    response = state.client("projects").get("/projects", params={"pageSize": 100})
    by_id = {project["projectId"]: project for project in response.get("projects", [])}
    choices = [
        {**by_id.get(assignment["projectId"], {"projectId": assignment["projectId"]}), "roles": assignment.get("roles")}
        for assignment in response.get("assignments") or []
    ]
    if not choices:
        raise click.ClickException("You are not assigned to any project. Ask a program owner to add you to one.")
    if not sys.stdin.isatty() and not state.interactive:
        raise click.UsageError("Pass a PROJECT_ID, or run this in a terminal to pick from a list.")

    choices.sort(key=lambda project: (project.get("projectName") or project["projectId"]).lower())
    current = state.profile.project_id
    rows = [
        {"#": str(number), " ": "*" if project["projectId"] == current else "", **project}
        for number, project in enumerate(choices, start=1)
    ]
    click.echo(output.table(rows, ["#", " ", "projectId", "projectName", "roles"]), err=True)
    default = next((row["#"] for row in rows if row[" "]), None)
    number = click.prompt("Project", type=click.IntRange(1, len(choices)), default=default, err=True)
    return choices[number - 1]
