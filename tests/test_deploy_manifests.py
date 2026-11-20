"""deploy/: invariants the Kubernetes and single-host manifests keep (docs/deployment.md)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from django.core.management import get_commands
from django.urls import resolve

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "kubernetes"
COMPOSE = ROOT / "deploy" / "docker" / "compose.yaml"
IMAGE = "ghcr.io/octoverse-id/octoreview:"


def manifests() -> list[dict]:
    found = []
    for path in sorted(K8S.glob("*.yaml")):
        found += [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
    return found


def by_kind(kind: str) -> list[dict]:
    return [doc for doc in manifests() if doc["kind"] == kind]


def pod_specs() -> list[tuple[str, dict]]:
    specs = []
    for doc in manifests():
        if doc["kind"] in ("Deployment", "Job"):
            specs.append((doc["metadata"]["name"], doc["spec"]["template"]["spec"]))
        elif doc["kind"] == "CronJob":
            specs.append(
                (doc["metadata"]["name"], doc["spec"]["jobTemplate"]["spec"]["template"]["spec"])
            )
    return specs


def compose_services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_every_object_lives_in_the_octoreview_namespace():
    for doc in manifests():
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "octoreview", doc["metadata"]["name"]


def test_one_image_tag_everywhere():
    images = {c["image"] for _, spec in pod_specs() for c in spec["containers"]}
    images |= {s["image"] for s in compose_services().values() if s["image"].startswith(IMAGE)}
    assert len(images) == 1, images
    assert next(iter(images)).startswith(IMAGE)


@pytest.mark.parametrize(("name", "spec"), pod_specs(), ids=[name for name, _ in pod_specs()])
def test_every_pod_is_hardened(name, spec):
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "octoreview"
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 1001
    for container in spec["containers"]:
        security = container["securityContext"]
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"]["drop"] == ["ALL"]
        assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
        # `args`, never `command`: the entrypoint's `manage.py check` must still run.
        assert "command" not in container
        assert {"configMapRef": {"name": "octoreview-config"}} in container["envFrom"]


def test_the_scheduled_jobs_run_real_commands_and_never_overlap():
    commands = get_commands()
    jobs = {}
    for doc in by_kind("CronJob"):
        assert doc["spec"]["concurrencyPolicy"] == "Forbid", doc["metadata"]["name"]
        args = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
        assert args[:2] == ["python", "manage.py"]
        assert args[2] in commands, args
        jobs[args[2]] = args[3:]
    assert jobs == {
        "dispatch_outbox_events": ["--limit", "100"],  # never --retry-failed on a schedule
        "cleanup_media": [],
        "prune_outbox_events": [],
        "recompute_ratings": ["--check"],
    }
    (migrate,) = by_kind("Job")
    assert migrate["spec"]["template"]["spec"]["containers"][0]["args"][2] == "migrate"


def test_probes_hit_real_routes_with_an_allowed_host():
    (deployment,) = by_kind("Deployment")
    (config,) = by_kind("ConfigMap")
    allowed = config["data"]["DJANGO_ALLOWED_HOSTS"].split(",")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        get = container[probe]["httpGet"]
        resolve(get["path"])
        assert get["path"].startswith("/api/v1/health/")
        hosts = [h["value"] for h in get["httpHeaders"] if h["name"] == "Host"]
        assert hosts and hosts[0] in allowed


def test_kubernetes_uses_a_cloud_backend_and_keeps_the_console_off():
    (config,) = by_kind("ConfigMap")
    assert config["data"]["OCTOREVIEW_STORAGE_BACKEND"] == "s3"
    assert config["data"]["DJANGO_DEBUG"] == "false"
    assert config["data"]["OCTOREVIEW_ADMIN_ENABLED"] == "false"


def test_the_single_host_stack_binds_loopback_and_migrates_first():
    services = compose_services()
    assert services["app"]["ports"] == ["127.0.0.1:8000:8000"]
    for name in ("app", "dispatcher", "cleanup"):
        assert services[name]["depends_on"]["migrate"]["condition"] == (
            "service_completed_successfully"
        )
