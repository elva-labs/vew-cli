"""Publishing API: products, their versions, and the AMIs they can be built from."""

import click

from vew_cli.commands.packaging import RELEASE_TYPES
from vew_cli.context import State, build_body, confirm, confirm_option, file_option, pass_state, require, wait_options
from vew_cli.waiting import WaitSpec

PRODUCT_TYPES = click.Choice(["WORKBENCH", "VIRTUAL_TARGET", "CONTAINER"], case_sensitive=False)

VERSION_CREATED = WaitSpec.of({"CREATED"}, {"FAILED", "RETIRED"})

PRODUCT_COLUMNS = ["productId", "productName", "productType", "technologyName", "status", "availableStages"]
VERSION_COLUMNS = ["versionId", "name", "versionType", "stages", "status", "recommendedVersion"]
AMI_COLUMNS = ["amiId", "amiName", "createDate"]

VERSION_TIMEOUT = 3600


@click.group()
def publishing():
    """Products and product versions (Publishing API)."""


def _project_path(state: State, *parts: str) -> str:
    return "/".join(["", "projects", state.project_id(), *parts])


def _request(state: State, method: str, *parts: str, **kwargs) -> dict:
    return state.client("publishing").request(method, _project_path(state, *parts), **kwargs)


def _get(state: State, *parts: str) -> dict:
    return _request(state, "GET", *parts)


def _versions(state: State, product_id: str) -> list[dict]:
    return _get(state, "products", product_id)["product"].get("versions") or []


def _read_template(path: str) -> str:
    # Sent as text: CloudFormation tags such as !Ref are not plain YAML, and VEW validates the template itself.
    with click.open_file(path) as handle:
        return handle.read()


def _technology_name(state: State, technology_id: str) -> str:
    path = "/".join(["", "projects", state.project_id(), "technologies"])
    technologies = state.client("projects").request("GET", path, params={"pageSize": 100}).get("technologies", [])
    for technology in technologies:
        if technology.get("id") == technology_id:
            return technology["name"]
    raise click.BadParameter(f"the project has no technology {technology_id}", param_hint="--technology-id")


# --- Products -------------------------------------------------------------------------------------------------------


@publishing.group()
def products():
    """Manage products."""


@products.command("list")
@pass_state
def list_products(state: State):
    """List the project's products."""
    state.emit(_get(state, "products"), "products", PRODUCT_COLUMNS)


@products.command("get")
@click.argument("product_id")
@pass_state
def get_product(state: State, product_id):
    """Show a product and its version summaries."""
    state.emit(_get(state, "products", product_id))


@products.command("create")
@click.option("--name", "productName")
@click.option("--description", "productDescription")
@click.option("--type", "productType", type=PRODUCT_TYPES)
@click.option("--technology-id", "technologyId", help="Versions publish into this technology's DEV portfolio.")
@click.option("--technology-name", "technologyName", help="Looked up from --technology-id when left out.")
@file_option
@pass_state
def create_product(state: State, file, **fields):
    """Create a product."""
    if fields["productType"]:
        fields["productType"] = fields["productType"].upper()
    body = require(build_body(file, **fields), "productName", "productType", "technologyId")
    body.setdefault("technologyName", _technology_name(state, body["technologyId"]))
    state.emit(_request(state, "POST", "products", json=body))


@products.command("archive")
@click.argument("product_id")
@confirm_option
@pass_state
def archive_product(state: State, product_id, yes):
    """Archive a product."""
    confirm(yes, f"Archive product {product_id}?")
    state.emit(_request(state, "DELETE", "products", product_id))


# --- Versions -------------------------------------------------------------------------------------------------------


@publishing.group()
def versions():
    """Manage product versions (asynchronous build and publish)."""


@versions.command("list")
@click.argument("product_id")
@pass_state
def list_versions(state: State, product_id):
    """List a product's versions."""
    state.emit({"versions": _versions(state, product_id)}, "versions", VERSION_COLUMNS)


@versions.command("get")
@click.argument("product_id")
@click.argument("version_id")
@pass_state
def get_version(state: State, product_id, version_id):
    """Show a product version, its distributions, and its template."""
    state.emit(_get(state, "products", product_id, "versions", version_id))


@versions.command("validate")
@click.argument("product_id")
@click.option("--template", "template_file", required=True, metavar="PATH", help="CloudFormation template (YAML).")
@pass_state
def validate_version(state: State, product_id, template_file):
    """Check a CloudFormation template without creating a version."""
    body = {"versionTemplateDefinition": _read_template(template_file)}
    state.emit(_request(state, "POST", "products", product_id, "versions", "validate", json=body))


@versions.command("create")
@click.argument("product_id")
@click.option("--release-type", "versionReleaseType", type=RELEASE_TYPES)
@click.option("--description", "productVersionDescription")
@click.option(
    "--template", "template_file", metavar="PATH", help="CloudFormation template for versionTemplateDefinition."
)
@click.option("--ami-id", "amiId", help="AMI for workbenches and virtual targets (see `vew publishing amis list`).")
@click.option("--image-tag", "imageTag", help="Image tag, for container products.")
@click.option("--image-digest", "imageDigest", help="Image digest, for container products.")
@click.option("--major-version", "majorVersionName", type=int, help="Starting major version for a MAJOR release.")
@file_option
@wait_options(VERSION_TIMEOUT, 15)
@pass_state
def create_version(state: State, product_id, file, template_file, wait_opts, **fields):
    """Create a product version. --wait polls until CREATED or FAILED."""
    if fields["versionReleaseType"]:
        fields["versionReleaseType"] = fields["versionReleaseType"].upper()
    body = build_body(file, **fields)
    if template_file:
        body["versionTemplateDefinition"] = _read_template(template_file)
    require(body, "versionReleaseType", "versionTemplateDefinition")

    # The create answers without the new version's ID, so find it in the product's version list.
    list_versions = lambda: _versions(state, product_id)  # noqa: E731
    before = {v["versionId"] for v in list_versions()}
    _request(state, "POST", "products", product_id, "versions", json=body)
    hint = f"Check `vew publishing versions list {product_id}` before creating it again."
    version_id = state.find_created(list_versions, "versionId", before, lambda v: True, "product version", hint)[
        "versionId"
    ]

    if wait_opts["wait"]:

        def fetch() -> dict:
            return next((v for v in list_versions() if v["versionId"] == version_id), {})

        state.wait(fetch, lambda v: v.get("status"), VERSION_CREATED, f"product version {version_id}", wait_opts)
    state.emit(_get(state, "products", product_id, "versions", version_id))


# --- AMIs -----------------------------------------------------------------------------------------------------------


@publishing.group()
def amis():
    """AMIs that product versions can use."""


@amis.command("list")
@pass_state
def list_amis(state: State):
    """List the AMIs the project's image builds produced."""
    state.emit(_get(state, "amis"), "amis", AMI_COLUMNS)
