import json

import pytest

from tests.conftest import provisioning_path

pytestmark = pytest.mark.usefixtures("static_token")

LAUNCH = ["provisioning", "provisioned", "launch", "--product-id", "prod-1", "--version-id", "vers-1"]


def test_list_products_passes_filters(api, run):
    api.add("GET", provisioning_path("available"), (200, {"availableProducts": []}))

    result = run("provisioning", "products", "list", "--type", "workbench", "--id", "prod-1", "--id", "prod-2")

    assert result.exit_code == 0, result.output
    params = api.requests[0].url.params
    assert params["productType"] == "workbench"
    assert params.get_list("filter") == ["prod-1", "prod-2"]


def test_list_products_requires_a_type(api, run):
    result = run("provisioning", "products", "list")

    assert result.exit_code == 2
    assert api.requests == []


def test_product_versions_require_stage_and_region(api, run):
    assert run("provisioning", "products", "versions", "prod-1", "--stage", "dev").exit_code == 2
    assert api.requests == []


def test_launch_finds_the_new_product_and_waits_for_running(api, run, sleeps):
    mine = {"provisionedProductId": "pp-0", "productId": "prod-1", "versionId": "vers-1"}
    launched = {"provisionedProductId": "pp-1", "productId": "prod-1", "versionId": "vers-1"}
    api.add(
        "GET",
        provisioning_path("provisioned"),
        (200, {"provisionedProducts": [mine]}),
        (200, {"provisionedProducts": [mine, launched]}),
    )
    api.add("POST", provisioning_path("provisioned"), (202, {}))
    api.add(
        "GET",
        provisioning_path("provisioned", "pp-1"),
        (200, {"provisionedProduct": {"status": "PROVISIONING"}}),
        (200, {"provisionedProduct": {"provisionedProductId": "pp-1", "status": "RUNNING"}}),
    )

    result = run(
        *LAUNCH,
        "--stage",
        "prod",
        "--region",
        "eu-west-1",
        "--param",
        "InstanceType=m8i.large",
        "--param",
        "Empty=",
        "--wait",
    )

    assert result.exit_code == 0, result.output
    assert api.bodies("POST", provisioning_path("provisioned")) == [
        {
            "stage": "PROD",
            "productId": "prod-1",
            "versionId": "vers-1",
            "region": "eu-west-1",
            "provisioningParameters": [{"key": "InstanceType", "value": "m8i.large"}, {"key": "Empty", "value": ""}],
        }
    ]
    lists = [r for r in api.requests if r.method == "GET" and r.url.path == provisioning_path("provisioned")]
    assert {r.url.params["productType"] for r in lists} == {"workbench"}
    assert json.loads(result.stdout)["provisionedProduct"]["status"] == "RUNNING"
    assert sleeps == [15]


def test_launch_always_sends_provisioning_parameters(api, run):
    launched = {"provisionedProductId": "pp-1", "productId": "prod-1", "versionId": "vers-1"}
    api.add(
        "GET",
        provisioning_path("provisioned"),
        (200, {"provisionedProducts": []}),
        (200, {"provisionedProducts": [launched]}),
    )
    api.add("POST", provisioning_path("provisioned"), (202, {}))
    api.add("GET", provisioning_path("provisioned", "pp-1"), (200, {"provisionedProduct": {"status": "PROVISIONING"}}))

    result = run(*LAUNCH, "--stage", "dev", "--region", "eu-west-1", "--type", "virtual_target")

    assert result.exit_code == 0, result.output
    assert api.bodies("POST", provisioning_path("provisioned"))[0]["provisioningParameters"] == []
    assert api.requests[0].url.params["productType"] == "virtualTarget"
    assert json.loads(result.stdout)["provisionedProduct"]["status"] == "PROVISIONING"


def test_launch_is_not_retried(api, run):
    api.add("GET", provisioning_path("provisioned"), (200, {"provisionedProducts": []}))
    api.add("POST", provisioning_path("provisioned"), (503, {"message": "Server error"}))

    result = run(*LAUNCH, "--stage", "DEV", "--region", "eu-west-1")

    assert result.exit_code == 1
    assert "HTTP 503: Server error" in result.stderr
    assert [r.method for r in api.requests] == ["GET", "POST"]


def test_launch_rejects_malformed_param(api, run):
    result = run(*LAUNCH, "--stage", "DEV", "--region", "r", "--param", "novalue")

    assert result.exit_code == 2
    assert api.requests == []


API_TYPES = {"workbench": "WORKBENCH", "virtualTarget": "VIRTUAL_TARGET", "container": "CONTAINER"}


def test_list_merges_your_products_of_every_type_and_filters_them(api, run):
    def by_type(request):
        product_type = API_TYPES[request.url.params["productType"]]
        products = [
            {
                "provisionedProductId": f"{product_type}-run",
                "provisionedProductType": product_type,
                "status": "RUNNING",
            },
            {
                "provisionedProductId": f"{product_type}-stop",
                "provisionedProductType": product_type,
                "status": "STOPPED",
            },
        ]
        return 200, {"provisionedProducts": products}

    api.add("GET", provisioning_path("provisioned"), by_type)

    result = run("provisioning", "provisioned", "list", "--status", "running")

    assert result.exit_code == 0, result.output
    ids = [p["provisionedProductId"] for p in json.loads(result.stdout)["provisionedProducts"]]
    assert ids == ["WORKBENCH-run", "VIRTUAL_TARGET-run", "CONTAINER-run"]


def test_project_wide_options_need_project_wide(api, run):
    result = run("provisioning", "provisioned", "list", "--owner", "JDOE", "--all")

    assert result.exit_code == 2
    assert "--owner, --all only apply with --project-wide" in result.stderr
    assert api.requests == []


def test_project_wide_all_follows_paging_key(api, run):
    api.add(
        "GET",
        provisioning_path("provisioned", "paginated"),
        (200, {"provisionedProducts": [{"provisionedProductId": "pp-1"}], "pagingKey": '{"pk": "a"}'}),
        (200, {"provisionedProducts": [{"provisionedProductId": "pp-2"}], "pagingKey": None}),
    )

    result = run(
        "provisioning",
        "provisioned",
        "list",
        "--project-wide",
        "--type",
        "virtual_target",
        "--status",
        "running",
        "--all",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "provisionedProducts": [{"provisionedProductId": "pp-1"}, {"provisionedProductId": "pp-2"}]
    }
    assert "pagingKey" not in api.requests[0].url.params
    assert api.requests[1].url.params["pagingKey"] == '{"pk": "a"}'
    assert api.requests[1].url.params["status"] == "RUNNING"
    assert api.requests[1].url.params["productType"] == "virtualTarget"


@pytest.mark.parametrize(
    ("command", "method", "suffix", "target"),
    [
        ("start", "PATCH", "start", "RUNNING"),
        ("stop", "PATCH", "stop", "STOPPED"),
    ],
)
def test_state_changes_wait_for_target(api, run, command, method, suffix, target):
    api.add(method, provisioning_path("provisioned", "pp-1", suffix), (200, {}))
    api.add("GET", provisioning_path("provisioned", "pp-1"), (200, {"provisionedProduct": {"status": target}}))

    result = run("provisioning", "provisioned", command, "pp-1", "--wait")

    assert result.exit_code == 0, result.output


def test_state_change_without_wait_shows_the_product(api, run):
    api.add("PATCH", provisioning_path("provisioned", "pp-1", "stop"), (200, {}))
    api.add("GET", provisioning_path("provisioned", "pp-1"), (200, {"provisionedProduct": {"status": "STOPPING"}}))

    result = run("provisioning", "provisioned", "stop", "pp-1")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"provisionedProduct": {"status": "STOPPING"}}


def test_remove_fails_wait_on_error_status(api, run):
    api.add("PUT", provisioning_path("provisioned", "pp-1", "remove"), (200, {}))
    api.add(
        "GET", provisioning_path("provisioned", "pp-1"), (200, {"provisionedProduct": {"status": "PROVISIONING_ERROR"}})
    )

    result = run("provisioning", "provisioned", "remove", "pp-1", "--yes", "--wait")

    assert result.exit_code == 3
