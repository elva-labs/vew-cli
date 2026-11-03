"""Provisioning API: the product catalogue and the signed-in user's provisioned products."""

import click

from vew_cli.context import (
    State,
    build_body,
    confirm,
    confirm_option,
    file_option,
    parse_key_values,
    pass_state,
    require,
    wait_options,
)
from vew_cli.waiting import WaitSpec

PRODUCT_TYPE_VALUES = ["WORKBENCH", "VIRTUAL_TARGET", "CONTAINER"]
PRODUCT_TYPES = click.Choice(PRODUCT_TYPE_VALUES, case_sensitive=False)
STAGES = click.Choice(["DEV", "QA", "PROD"], case_sensitive=False)
STATUSES = click.Choice(
    [
        "STARTING",
        "PROVISIONING",
        "STOPPING",
        "STOPPED",
        "RUNNING",
        "SHUTTING_DOWN",
        "DEPROVISIONING",
        "TERMINATED",
        "INSTANCE_ERROR",
        "PROVISIONING_ERROR",
        "UPDATING",
        "CONFIGURATION_IN_PROGRESS",
        "CONFIGURATION_FAILED",
    ],
    case_sensitive=False,
)

ERROR_STATUSES = {"PROVISIONING_ERROR", "INSTANCE_ERROR", "CONFIGURATION_FAILED"}
RUNNING = WaitSpec.of({"RUNNING"}, ERROR_STATUSES | {"TERMINATED", "STOPPED"})
STOPPED = WaitSpec.of({"STOPPED"}, ERROR_STATUSES | {"TERMINATED"})
TERMINATED = WaitSpec.of({"TERMINATED"}, ERROR_STATUSES)

PRODUCT_COLUMNS = ["productId", "productName", "productType", "availableStages", "availableRegions"]
VERSION_COLUMNS = ["versionId", "versionName", "isRecommendedVersion", "osVersion"]
PROVISIONED_COLUMNS = ["provisionedProductId", "productName", "versionName", "userId", "stage", "region", "status"]

LAUNCH_TIMEOUT = 3600
STATE_CHANGE_TIMEOUT = 1200


@click.group()
def provisioning():
    """Product catalogue and provisioned products (Provisioning API)."""


def _project_path(state: State, *parts: str) -> str:
    return "/".join(["", "projects", state.project_id(), "products", *parts])


def _request(state: State, method: str, *parts: str, **kwargs) -> dict:
    return state.client("provisioning").request(method, _project_path(state, *parts), **kwargs)


def _await_status(state: State, provisioned_product_id: str, wait_opts: dict, spec: WaitSpec) -> None:
    """Shows the provisioned product, once its status settles when --wait is given.

    The API answers launch, start, stop, and remove with an empty body, so the product is fetched instead.
    """
    fetch = lambda: _request(state, "GET", "provisioned", provisioned_product_id)  # noqa: E731
    if wait_opts["wait"]:
        response = state.wait(
            fetch,
            lambda r: r["provisionedProduct"].get("status"),
            spec,
            f"provisioned product {provisioned_product_id}",
            wait_opts,
        )
    else:
        response = fetch()
    state.emit(response)


def _api_product_type(product_type: str | None) -> str | None:
    """Spells a product type the way the API parses it: camelCase, so VIRTUAL_TARGET becomes virtualTarget.

    The API turns camelCase into SNAKE_CASE by adding an underscore before every capital, so it rejects the
    SNAKE_CASE values it returns.
    """
    if not product_type:
        return None
    first, *rest = product_type.lower().split("_")
    return first + "".join(word.capitalize() for word in rest)


def _own_provisioned(state: State, product_types: list[str]) -> list[dict]:
    items = []
    for product_type in product_types:
        response = _request(state, "GET", "provisioned", params={"productType": _api_product_type(product_type)})
        items += response.get("provisionedProducts", [])
    return items


# --- Catalogue ------------------------------------------------------------------------------------------------------


@provisioning.group()
def products():
    """Browse products available to the project."""


@products.command("list")
@click.option("--type", "product_type", type=PRODUCT_TYPES, required=True, help="Product type to list.")
@click.option("--id", "product_ids", multiple=True, help="Repeatable; only list these product IDs.")
@pass_state
def list_products(state: State, product_type, product_ids):
    """List products available to the project."""
    params = {"productType": _api_product_type(product_type), "filter": list(product_ids)}
    state.emit(_request(state, "GET", "available", params=params), "availableProducts", PRODUCT_COLUMNS)


@products.command("versions")
@click.argument("product_id")
@click.option("--stage", type=STAGES, required=True)
@click.option("--region", required=True)
@pass_state
def list_product_versions(state: State, product_id, stage, region):
    """List a product's versions that are ready to provision, with their parameters."""
    params = {"stage": stage.upper(), "region": region}
    state.emit(
        _request(state, "GET", product_id, "versions", params=params), "availableProductVersions", VERSION_COLUMNS
    )


# --- Provisioned products -------------------------------------------------------------------------------------------


@provisioning.group()
def provisioned():
    """Launch and operate provisioned products."""


@provisioned.command("list")
@click.option("--type", "product_type", type=PRODUCT_TYPES, help="Provisioned product type (default: all types).")
@click.option("--status", type=STATUSES)
@click.option("--stage", type=STAGES)
@click.option("--product-name")
@click.option("--version-name")
@click.option(
    "--project-wide",
    is_flag=True,
    help="Everyone's provisioned products in the project, including terminated ones. Needs PROGRAM_OWNER or ADMIN.",
)
@click.option("--owner", help="With --project-wide: user ID that owns the provisioned product.")
@click.option("--experimental/--no-experimental", default=None, help="With --project-wide.")
@click.option("--page-size", type=click.IntRange(1, 1000), help="With --project-wide.")
@click.option("--paging-key", help="With --project-wide: pagingKey from a previous page.")
@click.option("--all", "all_pages", is_flag=True, help="With --project-wide: follow pagingKey and return every page.")
@pass_state
def list_provisioned(
    state: State,
    product_type,
    status,
    stage,
    product_name,
    version_name,
    project_wide,
    owner,
    experimental,
    page_size,
    paging_key,
    all_pages,
):
    """List your provisioned products in the project, except terminated ones."""
    filters = {
        "productType": product_type.upper() if product_type else None,
        "status": status.upper() if status else None,
        "stage": stage.upper() if stage else None,
        "productName": product_name,
        "versionName": version_name,
    }
    if project_wide:
        response = _list_project_wide(state, filters, owner, experimental, page_size, paging_key, all_pages)
    else:
        project_wide_only = {
            "--owner": owner,
            "--experimental": experimental,
            "--page-size": page_size,
            "--paging-key": paging_key,
            "--all": all_pages or None,
        }
        given = [option for option, value in project_wide_only.items() if value is not None]
        if given:
            raise click.UsageError(f"{', '.join(given)} only apply with --project-wide.")
        product_types = [filters["productType"]] if product_type else PRODUCT_TYPE_VALUES
        wanted = {key: value for key, value in filters.items() if value is not None}
        items = [
            item
            for item in _own_provisioned(state, product_types)
            if all(_field(item, key) == value for key, value in wanted.items())
        ]
        response = {"provisionedProducts": items}
    state.emit(response, "provisionedProducts", PROVISIONED_COLUMNS)


def _field(item: dict, key: str):
    # The product type filter is called productType, but the provisioned product's field is provisionedProductType.
    return item.get("provisionedProductType") if key == "productType" else item.get(key)


def _list_project_wide(state: State, filters, owner, experimental, page_size, paging_key, all_pages) -> dict:
    params = {
        **filters,
        "productType": _api_product_type(filters["productType"]),
        "owner": owner,
        "experimental": None if experimental is None else str(experimental).lower(),
        "pageSize": page_size,
        "pagingKey": paging_key,
    }
    response = _request(state, "GET", "provisioned", "paginated", params=params)
    if all_pages:
        items = list(response.get("provisionedProducts", []))
        while response.get("pagingKey"):
            response = _request(
                state, "GET", "provisioned", "paginated", params={**params, "pagingKey": response["pagingKey"]}
            )
            items += response.get("provisionedProducts", [])
        response = {"provisionedProducts": items}
    return response


@provisioned.command("get")
@click.argument("provisioned_product_id")
@pass_state
def get_provisioned(state: State, provisioned_product_id):
    """Show one of your provisioned products with its outputs and version metadata."""
    state.emit(_request(state, "GET", "provisioned", provisioned_product_id))


@provisioned.command("launch")
@click.option("--product-id", "productId")
@click.option("--version-id", "versionId")
@click.option("--stage", type=STAGES)
@click.option("--region")
@click.option(
    "--type",
    "product_type",
    type=PRODUCT_TYPES,
    default="WORKBENCH",
    show_default=True,
    help="The product's type, used to find the new provisioned product.",
)
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE", help="Repeatable provisioning parameter.")
@file_option
@wait_options(LAUNCH_TIMEOUT, default_interval=15)
@pass_state
def launch(state: State, file, params, stage, product_type, wait_opts, **fields):
    """Provision a product for yourself. --wait polls until RUNNING or a failure status.

    Launch is not idempotent: if the request fails, check `provisioned list` before launching again.
    Use --file for additionalConfigurations.
    """
    body = build_body(file, stage=stage.upper() if stage else None, **fields)
    body["provisioningParameters"] = [*body.get("provisioningParameters", []), *parse_key_values(params, "--param")]
    require(body, "productId", "versionId", "stage", "region")
    product_types = [product_type.upper()]
    before = {item["provisionedProductId"] for item in _own_provisioned(state, product_types)}
    _request(state, "POST", "provisioned", json=body)
    created = state.find_created(
        lambda: _own_provisioned(state, product_types),
        "provisionedProductId",
        before,
        lambda item: item.get("productId") == body["productId"] and item.get("versionId") == body["versionId"],
        "provisioned product",
        "Check `vew provisioning provisioned list` before launching again.",
    )
    _await_status(state, created["provisionedProductId"], wait_opts, RUNNING)


@provisioned.command("start")
@click.argument("provisioned_product_id")
@wait_options(STATE_CHANGE_TIMEOUT)
@pass_state
def start(state: State, provisioned_product_id, wait_opts):
    """Start one of your stopped provisioned products. --wait polls until RUNNING."""
    _request(state, "PATCH", "provisioned", provisioned_product_id, "start")
    _await_status(state, provisioned_product_id, wait_opts, RUNNING)


@provisioned.command("stop")
@click.argument("provisioned_product_id")
@wait_options(STATE_CHANGE_TIMEOUT)
@pass_state
def stop(state: State, provisioned_product_id, wait_opts):
    """Stop one of your running provisioned products. --wait polls until STOPPED."""
    _request(state, "PATCH", "provisioned", provisioned_product_id, "stop")
    _await_status(state, provisioned_product_id, wait_opts, STOPPED)


@provisioned.command("remove")
@click.argument("provisioned_product_id")
@confirm_option
@wait_options(LAUNCH_TIMEOUT, default_interval=15)
@pass_state
def remove(state: State, provisioned_product_id, yes, wait_opts):
    """Deprovision one of your provisioned products. --wait polls until TERMINATED."""
    confirm(yes, f"Remove provisioned product {provisioned_product_id}? This terminates its instance.")
    _request(state, "PUT", "provisioned", provisioned_product_id, "remove")
    _await_status(state, provisioned_product_id, wait_opts, TERMINATED)
