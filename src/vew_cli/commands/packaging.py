"""Packaging API: components, recipes, pipelines, and image builds."""

import click
import yaml

from vew_cli.context import (
    State,
    build_body,
    confirm,
    confirm_option,
    file_option,
    pass_state,
    require,
    wait_options,
)
from vew_cli.waiting import WaitSpec

RELEASE_TYPES = click.Choice(["MAJOR", "MINOR", "PATCH"], case_sensitive=False)

VERSION_VALIDATED = WaitSpec.of({"VALIDATED"}, {"FAILED"})
VERSION_RETIRED = WaitSpec.of({"RETIRED"}, {"FAILED"})
PIPELINE_CREATED = WaitSpec.of({"CREATED"}, {"FAILED"})
PIPELINE_RETIRED = WaitSpec.of({"RETIRED"}, {"FAILED"})
IMAGE_CREATED = WaitSpec.of({"CREATED"}, {"FAILED", "RETIRED", "DELETED"})

COMPONENT_COLUMNS = ["componentId", "componentName", "componentPlatform", "componentSupportedArchitectures", "status"]
COMPONENT_VERSION_COLUMNS = [
    "componentVersionId",
    "componentVersionName",
    "softwareVendor",
    "softwareVersion",
    "status",
]
RECIPE_COLUMNS = ["recipeId", "recipeName", "recipePlatform", "recipeArchitecture", "recipeOsVersion", "status"]
RECIPE_VERSION_COLUMNS = ["recipeVersionId", "recipeVersionName", "recipeVersionVolumeSize", "status"]
PIPELINE_COLUMNS = ["pipelineId", "pipelineName", "recipeName", "recipeVersionName", "pipelineSchedule", "status"]
IMAGE_COLUMNS = ["imageId", "pipelineName", "imageBuildVersion", "status", "imageUpstreamId", "createDate"]

VERSION_TIMEOUT = 3600
PIPELINE_TIMEOUT = 1800
IMAGE_TIMEOUT = 4 * 3600


@click.group()
def packaging():
    """Components, recipes, pipelines, and image builds (Packaging API)."""


def _project_path(state: State, *parts: str) -> str:
    return "/".join(["", "projects", state.project_id(), *parts])


def _request(state: State, method: str, *parts: str, **kwargs) -> dict:
    return state.client("packaging").request(method, _project_path(state, *parts), **kwargs)


def _get(state: State, *parts: str) -> dict:
    return _request(state, "GET", *parts)


def _finish(
    state: State, response: dict, wait_opts: dict, label: str, fetch_parts: tuple[str, ...], key: str, spec: WaitSpec
) -> None:
    if wait_opts["wait"]:
        response = state.wait(lambda: _get(state, *fetch_parts), lambda r: r[key].get("status"), spec, label, wait_opts)
    state.emit(response)


def _ids(items: list[dict], id_key: str) -> set[str]:
    return {item[id_key] for item in items}


def _create(
    state: State,
    parts: tuple[str, ...],
    body: dict,
    rows_key: str,
    id_key: str,
    matches,
    label: str,
) -> str:
    """Creates a resource and returns its ID, which the Packaging API's create responses leave out."""
    list_items = lambda: _get(state, *parts).get(rows_key, [])  # noqa: E731
    before = _ids(list_items(), id_key)
    _request(state, "POST", *parts, json=body)
    hint = f"Check `vew packaging {label.replace(' ', '-')}s list` before creating it again."
    return state.find_created(list_items, id_key, before, matches, label, hint)[id_key]


# --- Components -----------------------------------------------------------------------------------------------------


@packaging.group()
def components():
    """Manage components."""


@components.command("list")
@pass_state
def list_components(state: State):
    """List the project's components."""
    state.emit(_get(state, "components"), "components", COMPONENT_COLUMNS)


@components.command("get")
@click.argument("component_id")
@pass_state
def get_component(state: State, component_id):
    """Show a component."""
    state.emit(_get(state, "components", component_id))


@components.command("create")
@click.option("--name", "componentName")
@click.option("--description", "componentDescription")
@click.option("--platform", "componentPlatform", help="For example Linux or Windows.")
@click.option("--arch", "componentSupportedArchitectures", multiple=True, help="Repeatable, for example amd64.")
@click.option(
    "--os-version", "componentSupportedOsVersions", multiple=True, help="Repeatable, for example 'Ubuntu 24'."
)
@file_option
@pass_state
def create_component(state: State, file, **fields):
    """Create a component."""
    body = require(build_body(file, **fields), *fields)
    state.emit(_request(state, "POST", "components", json=body))


@components.command("update")
@click.argument("component_id")
@click.option("--description", "componentDescription", required=True)
@pass_state
def update_component(state: State, component_id, componentDescription):
    """Update a component's description (the only mutable field)."""
    body = {"componentDescription": componentDescription}
    state.emit(_request(state, "PUT", "components", component_id, json=body))


@components.command("archive")
@click.argument("component_id")
@confirm_option
@pass_state
def archive_component(state: State, component_id, yes):
    """Archive a component."""
    confirm(yes, f"Archive component {component_id}?")
    state.emit(_request(state, "DELETE", "components", component_id))


# --- Component versions ---------------------------------------------------------------------------------------------


@packaging.group("component-versions")
def component_versions():
    """Manage component versions (asynchronous build and test)."""


COMPONENT_VERSION_OPTIONS = [
    click.option("--description", "componentVersionDescription"),
    click.option(
        "--definition",
        "definition_file",
        metavar="PATH",
        help="YAML component document (schemaVersion, phases, ...) for componentVersionYamlDefinition.",
    ),
    click.option(
        "--dependencies", "dependencies_file", metavar="PATH", help="JSON or YAML list of dependency entries."
    ),
    click.option("--software-vendor", "softwareVendor"),
    click.option("--software-version", "softwareVersion"),
    click.option("--license-dashboard", "licenseDashboard"),
    click.option("--notes", "notes"),
]


def _component_version_fields(command):
    return _apply(command, COMPONENT_VERSION_OPTIONS)


def _apply(command, options):
    for option in reversed(options):
        command = option(command)
    return command


def _component_version_body(file, definition_file, dependencies_file, fields) -> dict:
    body = build_body(file, **fields)
    if definition_file:
        body["componentVersionYamlDefinition"] = _read_yaml_document(definition_file)
    elif isinstance(body.get("componentVersionYamlDefinition"), dict):
        # The API takes the document as YAML text; a --file may hold it as a structured object instead.
        body["componentVersionYamlDefinition"] = yaml.safe_dump(body["componentVersionYamlDefinition"], sort_keys=False)
    if dependencies_file:
        body["componentVersionDependencies"] = _load_list(dependencies_file)
    return body


def _read_yaml_document(path: str) -> str:
    with click.open_file(path) as handle:
        text = handle.read()
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise click.BadParameter(f"{path} is not valid YAML: {error}", param_hint="--definition") from error
    if not isinstance(document, dict):
        raise click.BadParameter(f"{path} must contain a YAML mapping", param_hint="--definition")
    return text


def _load_list(path: str) -> list:
    with click.open_file(path) as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, list):
        raise click.BadParameter(f"{path} must contain a JSON/YAML list")
    return document


def _component_version_wait(state, component_id, version_id, response, wait_opts, spec):
    if not wait_opts["wait"]:
        state.emit(response)
        return

    # The version itself answers 500 until its build has stored the YAML definition, and never does once it fails,
    # so follow the status in the list and fetch the version only after it succeeds.
    def fetch() -> dict:
        versions = _get(state, "components", component_id, "versions").get("component_versions", [])
        return next((v for v in versions if v.get("componentVersionId") == version_id), {})

    state.wait(fetch, lambda v: v.get("status"), spec, f"component version {version_id}", wait_opts)
    state.emit(_get(state, "components", component_id, "versions", version_id))


@component_versions.command("list")
@click.argument("component_id")
@pass_state
def list_component_versions(state: State, component_id):
    """List a component's versions."""
    state.emit(_get(state, "components", component_id, "versions"), "component_versions", COMPONENT_VERSION_COLUMNS)


@component_versions.command("get")
@click.argument("component_id")
@click.argument("version_id")
@pass_state
def get_component_version(state: State, component_id, version_id):
    """Show a component version, including its structured definition."""
    state.emit(_get(state, "components", component_id, "versions", version_id))


@component_versions.command("create")
@click.argument("component_id")
@click.option("--release-type", "componentVersionReleaseType", type=RELEASE_TYPES)
@_component_version_fields
@file_option
@wait_options(VERSION_TIMEOUT)
@pass_state
def create_component_version(state: State, component_id, file, definition_file, dependencies_file, wait_opts, **fields):
    """Create a component version. --wait polls until VALIDATED or FAILED."""
    if fields["componentVersionReleaseType"]:
        fields["componentVersionReleaseType"] = fields["componentVersionReleaseType"].upper()
    body = _component_version_body(file, definition_file, dependencies_file, fields)
    require(
        body,
        "componentVersionDescription",
        "componentVersionReleaseType",
        "componentVersionYamlDefinition",
        "softwareVendor",
        "softwareVersion",
    )
    version_id = _create(
        state,
        ("components", component_id, "versions"),
        body,
        "component_versions",
        "componentVersionId",
        lambda v: v.get("componentVersionDescription") == body["componentVersionDescription"]
        and v.get("softwareVersion") == body["softwareVersion"],
        "component version",
    )
    _component_version_wait(
        state, component_id, version_id, {"componentVersionId": version_id}, wait_opts, VERSION_VALIDATED
    )


@component_versions.command("update")
@click.argument("component_id")
@click.argument("version_id")
@_component_version_fields
@file_option
@wait_options(VERSION_TIMEOUT)
@pass_state
def update_component_version(
    state: State, component_id, version_id, file, definition_file, dependencies_file, wait_opts, **fields
):
    """Replace a component version's content. --wait polls until VALIDATED or FAILED."""
    body = _component_version_body(file, definition_file, dependencies_file, fields)
    require(body, "componentVersionDescription", "componentVersionYamlDefinition", "softwareVendor", "softwareVersion")
    response = _request(state, "PUT", "components", component_id, "versions", version_id, json=body)
    _component_version_wait(state, component_id, version_id, response, wait_opts, VERSION_VALIDATED)


@component_versions.command("retire")
@click.argument("component_id")
@click.argument("version_id")
@confirm_option
@wait_options(VERSION_TIMEOUT)
@pass_state
def retire_component_version(state: State, component_id, version_id, yes, wait_opts):
    """Retire a component version. --wait polls until RETIRED or FAILED.

    Retiring a released version needs the ADMIN, PROGRAM_OWNER, or POWER_USER role in the project.
    """
    confirm(yes, f"Retire component version {version_id}?")
    response = _request(state, "DELETE", "components", component_id, "versions", version_id)
    _component_version_wait(state, component_id, version_id, response, wait_opts, VERSION_RETIRED)


@component_versions.command("release")
@click.argument("component_id")
@click.argument("version_id")
@pass_state
def release_component_version(state: State, component_id, version_id):
    """Release a VALIDATED component version (immutable afterwards)."""
    state.emit(_request(state, "POST", "components", component_id, "versions", version_id, json={}))


# --- Recipes --------------------------------------------------------------------------------------------------------


@packaging.group()
def recipes():
    """Manage recipes."""


@recipes.command("list")
@pass_state
def list_recipes(state: State):
    """List the project's recipes."""
    state.emit(_get(state, "recipes"), "recipes", RECIPE_COLUMNS)


@recipes.command("get")
@click.argument("recipe_id")
@pass_state
def get_recipe(state: State, recipe_id):
    """Show a recipe."""
    state.emit(_get(state, "recipes", recipe_id))


@recipes.command("create")
@click.option("--name", "recipeName")
@click.option("--description", "recipeDescription")
@click.option("--platform", "recipePlatform")
@click.option("--arch", "recipeArchitecture")
@click.option("--os-version", "recipeOsVersion")
@file_option
@pass_state
def create_recipe(state: State, file, **fields):
    """Create a recipe."""
    body = require(build_body(file, **fields), *fields)
    recipe_id = _create(
        state, ("recipes",), body, "recipes", "recipeId", lambda r: r.get("recipeName") == body["recipeName"], "recipe"
    )
    state.emit({"recipeId": recipe_id})


@recipes.command("archive")
@click.argument("recipe_id")
@confirm_option
@pass_state
def archive_recipe(state: State, recipe_id, yes):
    """Archive a recipe."""
    confirm(yes, f"Archive recipe {recipe_id}?")
    state.emit(_request(state, "DELETE", "recipes", recipe_id))


# --- Recipe versions ------------------------------------------------------------------------------------------------


@packaging.group("recipe-versions")
def recipe_versions():
    """Manage recipe versions (asynchronous build and test)."""


RECIPE_VERSION_OPTIONS = [
    click.option("--description", "recipeVersionDescription"),
    click.option("--volume-size", "recipeVersionVolumeSize", help="Root volume size in GiB."),
    click.option(
        "--components",
        "components_file",
        metavar="PATH",
        help="JSON or YAML list for recipeComponentsVersions: componentId, componentName, componentVersionId, "
        "componentVersionName, componentVersionType, order (from 1).",
    ),
    click.option(
        "--integrations", "integrations_file", metavar="PATH", help="JSON or YAML list for recipeVersionIntegrations."
    ),
]


def _recipe_version_fields(command):
    return _apply(command, RECIPE_VERSION_OPTIONS)


def _recipe_version_body(file, components_file, integrations_file, fields) -> dict:
    body = build_body(file, **fields)
    if body.get("recipeVersionVolumeSize") is not None:
        body["recipeVersionVolumeSize"] = str(body["recipeVersionVolumeSize"])
    if components_file:
        body["recipeComponentsVersions"] = _load_list(components_file)
    if integrations_file:
        body["recipeVersionIntegrations"] = _load_list(integrations_file)
    return body


def _recipe_version_wait(state, recipe_id, version_id, response, wait_opts, spec):
    _finish(
        state,
        response,
        wait_opts,
        f"recipe version {version_id}",
        ("recipes", recipe_id, "versions", version_id),
        "recipe_version",
        spec,
    )


@recipe_versions.command("list")
@click.argument("recipe_id")
@pass_state
def list_recipe_versions(state: State, recipe_id):
    """List a recipe's versions."""
    state.emit(_get(state, "recipes", recipe_id, "versions"), "recipe_versions", RECIPE_VERSION_COLUMNS)


@recipe_versions.command("get")
@click.argument("recipe_id")
@click.argument("version_id")
@pass_state
def get_recipe_version(state: State, recipe_id, version_id):
    """Show a recipe version with its components."""
    state.emit(_get(state, "recipes", recipe_id, "versions", version_id))


@recipe_versions.command("create")
@click.argument("recipe_id")
@click.option("--release-type", "recipeVersionReleaseType", type=RELEASE_TYPES)
@_recipe_version_fields
@file_option
@wait_options(VERSION_TIMEOUT)
@pass_state
def create_recipe_version(state: State, recipe_id, file, components_file, integrations_file, wait_opts, **fields):
    """Create a recipe version. --wait polls until VALIDATED or FAILED."""
    if fields["recipeVersionReleaseType"]:
        fields["recipeVersionReleaseType"] = fields["recipeVersionReleaseType"].upper()
    body = _recipe_version_body(file, components_file, integrations_file, fields)
    require(
        body,
        "recipeVersionDescription",
        "recipeVersionReleaseType",
        "recipeVersionVolumeSize",
        "recipeComponentsVersions",
    )
    version_id = _create(
        state,
        ("recipes", recipe_id, "versions"),
        body,
        "recipe_versions",
        "recipeVersionId",
        lambda v: v.get("recipeVersionDescription") == body["recipeVersionDescription"],
        "recipe version",
    )
    _recipe_version_wait(state, recipe_id, version_id, {"recipeVersionId": version_id}, wait_opts, VERSION_VALIDATED)


@recipe_versions.command("update")
@click.argument("recipe_id")
@click.argument("version_id")
@_recipe_version_fields
@file_option
@wait_options(VERSION_TIMEOUT)
@pass_state
def update_recipe_version(
    state: State, recipe_id, version_id, file, components_file, integrations_file, wait_opts, **fields
):
    """Replace a recipe version's content. --wait polls until VALIDATED or FAILED."""
    body = _recipe_version_body(file, components_file, integrations_file, fields)
    require(body, "recipeVersionDescription", "recipeVersionVolumeSize", "recipeComponentsVersions")
    response = _request(state, "PUT", "recipes", recipe_id, "versions", version_id, json=body)
    _recipe_version_wait(state, recipe_id, version_id, response, wait_opts, VERSION_VALIDATED)


@recipe_versions.command("retire")
@click.argument("recipe_id")
@click.argument("version_id")
@confirm_option
@wait_options(VERSION_TIMEOUT)
@pass_state
def retire_recipe_version(state: State, recipe_id, version_id, yes, wait_opts):
    """Retire a recipe version. --wait polls until RETIRED or FAILED.

    Retiring a released version needs the ADMIN, PROGRAM_OWNER, or POWER_USER role in the project.
    """
    confirm(yes, f"Retire recipe version {version_id}?")
    response = _request(state, "DELETE", "recipes", recipe_id, "versions", version_id)
    _recipe_version_wait(state, recipe_id, version_id, response, wait_opts, VERSION_RETIRED)


@recipe_versions.command("release")
@click.argument("recipe_id")
@click.argument("version_id")
@pass_state
def release_recipe_version(state: State, recipe_id, version_id):
    """Release a VALIDATED recipe version whose component versions are all released."""
    state.emit(_request(state, "POST", "recipes", recipe_id, "versions", version_id, json={}))


# --- Pipelines ------------------------------------------------------------------------------------------------------


@packaging.group()
def pipelines():
    """Manage image pipelines."""


def _pipeline_wait(state, pipeline_id, response, wait_opts, spec):
    _finish(state, response, wait_opts, f"pipeline {pipeline_id}", ("pipelines", pipeline_id), "pipeline", spec)


@pipelines.command("list")
@pass_state
def list_pipelines(state: State):
    """List the project's pipelines."""
    state.emit(_get(state, "pipelines"), "pipelines", PIPELINE_COLUMNS)


@pipelines.command("get")
@click.argument("pipeline_id")
@pass_state
def get_pipeline(state: State, pipeline_id):
    """Show a pipeline."""
    state.emit(_get(state, "pipelines", pipeline_id))


@pipelines.command("create")
@click.option("--name", "pipelineName")
@click.option("--description", "pipelineDescription")
@click.option("--recipe-id", "recipeId")
@click.option("--recipe-version-id", "recipeVersionId", help="A released version of the recipe.")
@click.option("--instance-type", "buildInstanceTypes", multiple=True, help="Repeatable build instance type.")
@click.option("--schedule", "pipelineSchedule", help="Six-field schedule without cron(...), e.g. '0 0 * * ? *'.")
@click.option("--product-id", "productId", help="Associate built images with a product.")
@file_option
@wait_options(PIPELINE_TIMEOUT)
@pass_state
def create_pipeline(state: State, file, wait_opts, **fields):
    """Create a pipeline. --wait polls until CREATED or FAILED."""
    body = build_body(file, **fields)
    require(
        body,
        "pipelineName",
        "pipelineDescription",
        "recipeId",
        "recipeVersionId",
        "buildInstanceTypes",
        "pipelineSchedule",
    )
    pipeline_id = _create(
        state,
        ("pipelines",),
        body,
        "pipelines",
        "pipelineId",
        lambda p: p.get("pipelineName") == body["pipelineName"],
        "pipeline",
    )
    _pipeline_wait(state, pipeline_id, {"pipelineId": pipeline_id}, wait_opts, PIPELINE_CREATED)


@pipelines.command("update")
@click.argument("pipeline_id")
@click.option("--recipe-version-id", "recipeVersionId")
@click.option("--instance-type", "buildInstanceTypes", multiple=True)
@click.option("--schedule", "pipelineSchedule")
@click.option("--product-id", "productId", help="Omitting this clears any existing product association.")
@file_option
@wait_options(PIPELINE_TIMEOUT)
@pass_state
def update_pipeline(state: State, pipeline_id, file, wait_opts, **fields):
    """Update a pipeline. --wait polls until CREATED or FAILED.

    As in the UI, omitting --product-id clears the pipeline's product association.
    """
    body = build_body(file, **fields)
    if not body:
        raise click.UsageError("Nothing to update.")
    body["pipelineId"] = pipeline_id
    response = _request(state, "PUT", "pipelines", pipeline_id, json=body)
    _pipeline_wait(state, pipeline_id, response, wait_opts, PIPELINE_CREATED)


@pipelines.command("retire")
@click.argument("pipeline_id")
@confirm_option
@wait_options(PIPELINE_TIMEOUT)
@pass_state
def retire_pipeline(state: State, pipeline_id, yes, wait_opts):
    """Retire a pipeline. --wait polls until RETIRED or FAILED."""
    confirm(yes, f"Retire pipeline {pipeline_id}?")
    response = _request(state, "DELETE", "pipelines", pipeline_id)
    _pipeline_wait(state, pipeline_id, response, wait_opts, PIPELINE_RETIRED)


# --- Images ---------------------------------------------------------------------------------------------------------


@packaging.group()
def images():
    """Start image builds and inspect their results."""


@images.command("list")
@pass_state
def list_images(state: State):
    """List the project's images."""
    state.emit(_get(state, "images"), "images", IMAGE_COLUMNS)


@images.command("get")
@click.argument("image_id")
@pass_state
def get_image(state: State, image_id):
    """Show an image build; imageUpstreamId is the resulting AMI ID."""
    state.emit(_get(state, "images", image_id))


@images.command("build")
@click.option("--pipeline-id", required=True, help="A pipeline in CREATED status.")
@wait_options(IMAGE_TIMEOUT, default_interval=30)
@pass_state
def build_image(state: State, pipeline_id, wait_opts):
    """Start an image build. --wait polls until CREATED or FAILED.

    Every call starts another build; check `images list` before retrying a failed call.
    """
    image_id = _create(
        state,
        ("images",),
        {"pipelineId": pipeline_id},
        "images",
        "imageId",
        lambda i: i.get("pipelineId") == pipeline_id,
        "image",
    )
    _finish(state, {"imageId": image_id}, wait_opts, f"image {image_id}", ("images", image_id), "image", IMAGE_CREATED)
