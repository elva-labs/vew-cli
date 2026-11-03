import json

import pytest

from tests.conftest import PROJECT_ID, publishing_path

pytestmark = pytest.mark.usefixtures("static_token")

TEMPLATE = """Parameters:
  InstanceType:
    Type: String
Resources:
  Workbench:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref InstanceType
"""


def _product(*versions):
    return 200, {"product": {"productId": "prod-1", "versions": list(versions)}}


def test_create_product_looks_up_the_technology_name(api, run):
    api.add(
        "GET",
        f"/projects/projects/{PROJECT_ID}/technologies",
        (200, {"technologies": [{"id": "tech-1", "name": "Embedded"}]}),
    )
    api.add("POST", publishing_path("products"), (200, {"productId": "prod-1"}))

    result = run(
        "publishing",
        "products",
        "create",
        "--name",
        "FreeRTOS",
        "--type",
        "virtual_target",
        "--technology-id",
        "tech-1",
    )

    assert result.exit_code == 0, result.output
    assert api.bodies("POST", publishing_path("products")) == [
        {
            "productName": "FreeRTOS",
            "productType": "VIRTUAL_TARGET",
            "technologyId": "tech-1",
            "technologyName": "Embedded",
        }
    ]
    assert json.loads(result.stdout) == {"productId": "prod-1"}


def test_create_product_rejects_an_unknown_technology(api, run):
    api.add("GET", f"/projects/projects/{PROJECT_ID}/technologies", (200, {"technologies": []}))

    result = run("publishing", "products", "create", "--name", "x", "--type", "workbench", "--technology-id", "t")

    assert result.exit_code == 2
    assert "the project has no technology t" in result.stderr
    assert [r.method for r in api.requests] == ["GET"]


def test_create_version_sends_the_template_as_text_and_waits(api, run, tmp_path, sleeps):
    template = tmp_path / "product.yaml"
    template.write_text(TEMPLATE)
    old = {"versionId": "vers-0", "status": "CREATED"}
    api.add(
        "GET",
        publishing_path("products", "prod-1"),
        _product(old),
        _product(old, {"versionId": "vers-1", "status": "PROCESSING"}),
        _product(old, {"versionId": "vers-1", "status": "PROCESSING"}),
        _product(old, {"versionId": "vers-1", "status": "CREATED"}),
    )
    api.add("POST", publishing_path("products", "prod-1", "versions"), (200, {}))
    api.add(
        "GET", publishing_path("products", "prod-1", "versions", "vers-1"), (200, {"version": {"versionId": "vers-1"}})
    )

    result = run(
        "publishing",
        "versions",
        "create",
        "prod-1",
        "--release-type",
        "major",
        "--description",
        "Initial",
        "--ami-id",
        "ami-1",
        "--template",
        str(template),
        "--wait",
    )

    assert result.exit_code == 0, result.output
    assert api.bodies("POST", publishing_path("products", "prod-1", "versions")) == [
        {
            "versionReleaseType": "MAJOR",
            "productVersionDescription": "Initial",
            "amiId": "ami-1",
            "versionTemplateDefinition": TEMPLATE,
        }
    ]
    assert json.loads(result.stdout) == {"version": {"versionId": "vers-1"}}
    assert sleeps == [15]


def test_create_version_fails_wait_when_the_build_fails(api, run, tmp_path):
    template = tmp_path / "product.yaml"
    template.write_text(TEMPLATE)
    api.add(
        "GET",
        publishing_path("products", "prod-1"),
        _product(),
        _product({"versionId": "vers-1", "status": "FAILED"}),
    )
    api.add("POST", publishing_path("products", "prod-1", "versions"), (200, {}))

    result = run(
        "publishing", "versions", "create", "prod-1", "--release-type", "major", "--template", str(template), "--wait"
    )

    assert result.exit_code == 3
    assert "finished with status FAILED" in result.stderr


def test_create_version_requires_a_template(api, run):
    result = run("publishing", "versions", "create", "prod-1", "--release-type", "major")

    assert result.exit_code == 2
    assert api.requests == []
