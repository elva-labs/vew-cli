import json

import pytest

from tests.conftest import packaging_path

pytestmark = pytest.mark.usefixtures("static_token")


def test_list_components_renders_table_with_bearer_token(api, run):
    api.add(
        "GET",
        packaging_path("components"),
        (
            200,
            {
                "components": [
                    {
                        "componentId": "comp-1",
                        "componentName": "agent",
                        "componentPlatform": "Linux",
                        "componentSupportedArchitectures": ["amd64", "arm64"],
                        "status": "CREATED",
                    },
                ]
            },
        ),
    )

    result = run("-o", "table", "packaging", "components", "list")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "componentId  componentName  componentPlatform  componentSupportedArchitectures  status",
        "comp-1       agent          Linux              amd64,arm64                      CREATED",
    ]
    assert api.requests[0].headers["Authorization"] == "Bearer static-token"


def test_create_component(api, run):
    api.add("POST", packaging_path("components"), (201, {"componentId": "comp-1"}))

    result = run(
        "packaging",
        "components",
        "create",
        "--name",
        "agent",
        "--description",
        "d",
        "--platform",
        "Linux",
        "--arch",
        "amd64",
        "--os-version",
        "Ubuntu 24",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"componentId": "comp-1"}
    assert "Idempotency-Key" not in api.requests[0].headers
    assert api.bodies("POST", packaging_path("components")) == [
        {
            "componentName": "agent",
            "componentDescription": "d",
            "componentPlatform": "Linux",
            "componentSupportedArchitectures": ["amd64"],
            "componentSupportedOsVersions": ["Ubuntu 24"],
        }
    ]


def test_create_reports_missing_required_fields_without_calling_api(api, run):
    result = run("packaging", "components", "create", "--name", "agent")

    assert result.exit_code == 2
    assert "componentDescription" in result.stderr
    assert api.requests == []


def test_writes_are_not_replayed_on_503(api, run, sleeps):
    api.add("PUT", packaging_path("components", "comp-1"), (503, {"message": "Server error"}))

    result = run("packaging", "components", "update", "comp-1", "--description", "new")

    assert result.exit_code == 1
    assert "HTTP 503: Server error" in result.stderr
    assert len(api.requests) == 1
    assert sleeps == []


def test_get_retries_throttling(api, run, sleeps):
    api.add("GET", packaging_path("pipelines"), (429, {"message": "Too Many Requests"}), (200, {"pipelines": []}))

    result = run("packaging", "pipelines", "list")

    assert result.exit_code == 0, result.output
    assert sleeps == [1]
    assert "retrying in 1s" in result.stderr


def test_validation_errors_are_reported(api, run):
    api.add(
        "POST",
        packaging_path("components"),
        (400, {"message": "Invalid request body", "validationError": "componentName is required"}),
    )

    result = run(
        "packaging",
        "components",
        "create",
        "-f",
        "-",
        input=json.dumps(
            {
                k: "x"
                for k in [
                    "componentName",
                    "componentDescription",
                    "componentPlatform",
                    "componentSupportedArchitectures",
                    "componentSupportedOsVersions",
                ]
            }
        ),
    )

    assert result.exit_code == 1
    assert "HTTP 400: Invalid request body: componentName is required" in result.stderr


def test_component_version_create_finds_the_new_version_and_waits_for_validated(api, run, sleeps, tmp_path):
    body = tmp_path / "version.yaml"
    body.write_text(
        "componentVersionDescription: from file\n"
        "componentVersionReleaseType: MAJOR\n"
        "softwareVendor: Example\n"
        "softwareVersion: '1.0.0'\n"
    )
    definition = tmp_path / "definition.yaml"
    definition_text = "schemaVersion: '1.0'\nphases:\n  - name: build\n    steps: []\n"
    definition.write_text(definition_text)
    versions_path = packaging_path("components", "comp-1", "versions")
    existing = {"componentVersionId": "vers-0", "componentVersionDescription": "from flag", "softwareVersion": "1.0.0"}
    other = {"componentVersionId": "vers-2", "componentVersionDescription": "someone else", "softwareVersion": "2.0"}
    created = {"componentVersionId": "vers-1", "componentVersionDescription": "from flag", "softwareVersion": "1.0.0"}
    api.add(
        "GET",
        versions_path,
        (200, {"component_versions": [existing]}),
        (200, {"component_versions": [existing, other, created]}),
        (200, {"component_versions": [existing, other, {**created, "status": "CREATING"}]}),
        (200, {"component_versions": [existing, other, {**created, "status": "TESTING"}]}),
        (200, {"component_versions": [existing, other, {**created, "status": "VALIDATED"}]}),
    )
    api.add("POST", versions_path, (200, {}))
    api.add(
        "GET",
        packaging_path("components", "comp-1", "versions", "vers-1"),
        (200, {"component_version": {"componentVersionId": "vers-1", "status": "VALIDATED"}}),
    )

    result = run(
        "packaging",
        "component-versions",
        "create",
        "comp-1",
        "-f",
        str(body),
        "--definition",
        str(definition),
        "--description",
        "from flag",
        "--wait",
        "--interval",
        "2",
    )

    assert result.exit_code == 0, result.output
    sent = api.bodies("POST", versions_path)[0]
    assert sent["componentVersionDescription"] == "from flag"
    assert sent["componentVersionYamlDefinition"] == definition_text
    assert json.loads(result.stdout)["component_version"]["status"] == "VALIDATED"
    assert sleeps == [2, 2]
    assert "component version vers-1: TESTING" in result.stderr


def test_structured_definition_in_file_is_sent_as_yaml(api, run):
    versions_path = packaging_path("components", "comp-1", "versions")
    created = {"componentVersionId": "vers-1", "componentVersionDescription": "d", "softwareVersion": "1"}
    api.add("GET", versions_path, (200, {"component_versions": []}), (200, {"component_versions": [created]}))
    api.add("POST", versions_path, (200, {}))
    document = {
        "componentVersionDescription": "d",
        "componentVersionReleaseType": "MINOR",
        "softwareVendor": "v",
        "softwareVersion": "1",
        "componentVersionYamlDefinition": {"schemaVersion": "1.0", "phases": []},
    }

    result = run("packaging", "component-versions", "create", "comp-1", "-f", "-", input=json.dumps(document))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"componentVersionId": "vers-1"}
    sent = api.bodies("POST", versions_path)[0]
    assert sent["componentVersionYamlDefinition"] == "schemaVersion: '1.0'\nphases: []\n"


def test_create_exits_5_when_the_new_resource_is_ambiguous(api, run):
    api.add(
        "GET",
        packaging_path("pipelines"),
        (200, {"pipelines": []}),
        (
            200,
            {
                "pipelines": [
                    {"pipelineId": "pipe-1", "pipelineName": "p"},
                    {"pipelineId": "pipe-2", "pipelineName": "p"},
                ]
            },
        ),
    )
    api.add("POST", packaging_path("pipelines"), (200, {}))

    result = run(
        "packaging",
        "pipelines",
        "create",
        "--name",
        "p",
        "--description",
        "d",
        "--recipe-id",
        "reci-1",
        "--recipe-version-id",
        "vers-1",
        "--instance-type",
        "m8i.large",
        "--schedule",
        "0 0 * * ? *",
    )

    assert result.exit_code == 5
    assert "2 new ones match it (pipe-1, pipe-2)" in result.stderr
    assert "vew packaging pipelines list" in result.stderr


def test_recipe_create_reports_the_new_id(api, run):
    api.add(
        "GET",
        packaging_path("recipes"),
        (200, {"recipes": []}),
        (200, {"recipes": [{"recipeId": "reci-1", "recipeName": "r"}]}),
    )
    api.add("POST", packaging_path("recipes"), (200, {}))

    result = run(
        "packaging",
        "recipes",
        "create",
        "--name",
        "r",
        "--description",
        "d",
        "--platform",
        "Linux",
        "--arch",
        "amd64",
        "--os-version",
        "Ubuntu 24",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"recipeId": "reci-1"}


def test_wait_exits_3_when_version_fails(api, run):
    api.add("PUT", packaging_path("recipes", "reci-1", "versions", "vers-1"), (200, {}))
    api.add(
        "GET",
        packaging_path("recipes", "reci-1", "versions", "vers-1"),
        (200, {"recipe_version": {"status": "FAILED"}}),
    )

    result = run(
        "packaging",
        "recipe-versions",
        "update",
        "reci-1",
        "vers-1",
        "--description",
        "d",
        "--volume-size",
        "30",
        "-f",
        "-",
        "--wait",
        input=json.dumps({"recipeComponentsVersions": [{"componentId": "comp-1"}]}),
    )

    assert result.exit_code == 3
    assert "finished with status FAILED" in result.stderr
    sent = api.bodies("PUT", packaging_path("recipes", "reci-1", "versions", "vers-1"))[0]
    assert sent["recipeVersionVolumeSize"] == "30"


def test_image_build_finds_the_new_image_and_wait_exits_4_on_timeout(api, run):
    api.add(
        "GET",
        packaging_path("images"),
        (200, {"images": [{"imageId": "imag-0", "pipelineId": "pipe-1"}]}),
        (
            200,
            {"images": [{"imageId": "imag-0", "pipelineId": "pipe-1"}, {"imageId": "imag-1", "pipelineId": "pipe-1"}]},
        ),
    )
    api.add("POST", packaging_path("images"), (200, {}))
    api.add("GET", packaging_path("images", "imag-1"), (200, {"image": {"status": "CREATING"}}))

    result = run("packaging", "images", "build", "--pipeline-id", "pipe-1", "--wait", "--timeout", "0")

    assert result.exit_code == 4
    assert api.bodies("POST", packaging_path("images")) == [{"pipelineId": "pipe-1"}]
    assert "image imag-1: CREATING" in result.stderr


def test_pipeline_update_sends_the_pipeline_id(api, run):
    api.add("PUT", packaging_path("pipelines", "pipe-1"), (200, {}))

    result = run("packaging", "pipelines", "update", "pipe-1", "--schedule", "0 1 * * ? *")

    assert result.exit_code == 0, result.output
    assert api.bodies("PUT", packaging_path("pipelines", "pipe-1")) == [
        {"pipelineSchedule": "0 1 * * ? *", "pipelineId": "pipe-1"}
    ]


def test_retire_requires_confirmation(api, run):
    declined = run("packaging", "pipelines", "retire", "pipe-1", input="n\n")
    assert declined.exit_code == 1
    assert api.requests == []

    api.add("DELETE", packaging_path("pipelines", "pipe-1"), (200, {}))
    accepted = run("packaging", "pipelines", "retire", "pipe-1", "--yes")
    assert accepted.exit_code == 0, accepted.output
    assert [r.method for r in api.requests] == ["DELETE"]


@pytest.mark.parametrize(
    ("group", "parent"),
    [("component-versions", ("components", "comp-1")), ("recipe-versions", ("recipes", "reci-1"))],
)
def test_release_posts_an_empty_body_to_the_version(api, run, group, parent):
    api.add("POST", packaging_path(*parent, "versions", "vers-1"), (200, {}))

    result = run("packaging", group, "release", parent[1], "vers-1")

    assert result.exit_code == 0, result.output
    assert api.bodies("POST", packaging_path(*parent, "versions", "vers-1")) == [{}]


def test_requires_project(api, run, monkeypatch):
    monkeypatch.delenv("VEW_PROJECT_ID")

    result = run("packaging", "components", "list")

    assert result.exit_code == 2
    assert "No project selected" in result.stderr


def test_project_flag_overrides_environment(api, run):
    api.add("GET", "/packaging/projects/proj-2/recipes", (200, {"recipes": []}))

    result = run("--project", "proj-2", "packaging", "recipes", "list")

    assert result.exit_code == 0, result.output


def test_component_version_wait_reports_failure_without_fetching_the_version(api, run):
    versions_path = packaging_path("components", "comp-1", "versions")
    api.add("PUT", packaging_path("components", "comp-1", "versions", "vers-1"), (200, {}))
    api.add("GET", versions_path, (200, {"component_versions": [{"componentVersionId": "vers-1", "status": "FAILED"}]}))

    result = run(
        "packaging",
        "component-versions",
        "update",
        "comp-1",
        "vers-1",
        "--description",
        "d",
        "--software-vendor",
        "v",
        "--software-version",
        "1",
        "-f",
        "-",
        "--wait",
        input=json.dumps({"componentVersionYamlDefinition": "schemaVersion: 1.0\nphases: []\n"}),
    )

    assert result.exit_code == 3
    assert "component version vers-1 finished with status FAILED" in result.stderr
    assert [r.url.path for r in api.requests if r.method == "GET"] == [versions_path]
