"""Tests estáticos de S16: CI de GitHub Actions, Pages, cobertura y pre-commit.

El workflow y las Pages no se pueden ejecutar en local, así que estos tests parsean los
archivos de configuración y verifican todo lo verificable de cada criterio de aceptación
(docs/BACKLOG.md §S16). Un fallo acá es un bug de los archivos de CI, no del runtime.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
SITE_PATH = ROOT / "site" / "index.html"
PRE_COMMIT_PATH = ROOT / ".pre-commit-config.yaml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
UV_LOCK_PATH = ROOT / "uv.lock"
MAKEFILE_PATH = ROOT / "Makefile"
GITIGNORE_PATH = ROOT / ".gitignore"
DEPENDABOT_PATH = ROOT / ".github" / "dependabot.yml"

#: `owner/repo@<sha de 40>`: pinneo por commit (S16 SEC-1, resuelto en S17).
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
#: Línea `uses:` cruda con la versión exacta en el comentario: `owner/repo@<sha> # vX.Y.Z`.
PINNED_ACTION_LINE = re.compile(r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")
#: Versión exacta `x.y.z` (sin rangos ni prefijos `^`/`~`).
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

MAIN_PUSH_CONDITION = "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} no es un mapping YAML"
    return data


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return _load_yaml(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


@pytest.fixture(scope="module")
def pre_commit() -> dict[str, Any]:
    return _load_yaml(PRE_COMMIT_PATH)


@pytest.fixture(scope="module")
def site_html() -> str:
    return SITE_PATH.read_text(encoding="utf-8")


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """`on:` se parsea como el booleano `True` con YAML 1.1, así que se acepta cualquiera."""
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "el workflow no declara `on:`"
    return triggers


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps", []))


def _runs(job: dict[str, Any]) -> list[str]:
    """Comandos `run:` del job, con los espacios normalizados (los `>-` pliegan líneas)."""
    return [re.sub(r"\s+", " ", str(step.get("run", ""))) for step in _steps(job)]


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(_runs(job))


def _uses(job: dict[str, Any]) -> list[str]:
    return [str(step["uses"]) for step in _steps(job) if "uses" in step]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _workflow_uses(workflow: dict[str, Any]) -> list[str]:
    return [uses for job in workflow["jobs"].values() for uses in _uses(job)]


# --- Triggers y concurrencia -------------------------------------------------


def test_workflow_triggers_on_pull_request_and_push_to_main(workflow: dict[str, Any]) -> None:
    """Criterio: trigger en `pull_request` y `push` a `main`."""
    triggers = _triggers(workflow)

    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]


def test_workflow_concurrency_cancels_stale_pull_request_runs(
    workflow: dict[str, Any],
) -> None:
    """Criterio: `concurrency` por ref que cancela las corridas viejas del mismo PR."""
    concurrency = workflow["concurrency"]

    assert "github.ref" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


# --- Permisos ----------------------------------------------------------------


def test_workflow_permissions_are_contents_read_only(workflow: dict[str, Any]) -> None:
    """Criterio: `permissions: contents: read` a nivel workflow."""
    assert workflow["permissions"] == {"contents": "read"}


def test_pages_permissions_live_only_in_the_deploy_job(jobs: dict[str, Any]) -> None:
    """Criterio: solo el job de deploy agrega `pages: write` e `id-token: write`."""
    assert jobs["pages-deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
    for name, job in jobs.items():
        if name == "pages-deploy":
            continue
        permissions = job.get("permissions", {})
        assert "pages" not in permissions, f"el job {name} pide permisos de Pages"
        assert "id-token" not in permissions, f"el job {name} pide id-token"

    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")


# --- Job `test` --------------------------------------------------------------


def test_test_job_uses_a_healthy_postgres_16_alpine_service(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: service `postgres:16-alpine` con healthcheck que crea `agrobot_test`."""
    service = jobs["test"]["services"]["postgres"]

    assert service["image"] == "postgres:16-alpine"
    assert service["env"]["POSTGRES_DB"] == "agrobot_test"
    assert any("5432:5432" in str(port) for port in service["ports"])
    options = str(service["options"])
    assert "pg_isready" in options
    assert "--health-retries" in options

    # `TEST_DATABASE_URL` apunta al service (mismo puerto y base).
    test_url = workflow["env"]["TEST_DATABASE_URL"]
    assert "localhost:5432/agrobot_test" in test_url


def test_test_job_installs_uv_with_cache_and_syncs_frozen(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: `astral-sh/setup-uv` con cache de uv y `uv sync --frozen`."""
    steps = _steps(jobs["test"])
    setup_uv = [
        step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]

    assert len(setup_uv) == 1
    assert setup_uv[0]["with"]["enable-cache"] is True
    assert any("uv sync --frozen" in run for run in _runs(jobs["test"]))


def test_test_job_runs_lint_typecheck_and_the_contract_before_the_suite(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: ruff, ruff format, mypy y el contrato OpenAPI como pasos previos a pytest."""
    runs = _runs(jobs["test"])

    def index_of(needle: str) -> int:
        matches = [position for position, run in enumerate(runs) if needle in run]
        assert matches, f"no hay un paso que corra {needle!r}"
        return matches[0]

    contract = index_of("tests/test_openapi_contract.py")
    suite = index_of("--cov=app")
    assert contract < suite, "el contrato OpenAPI debe correr antes de la suite completa"
    assert "--cov" not in runs[contract], "el contrato OpenAPI no debe mezclarse con la suite"

    for needle in ("ruff check", "ruff format --check", "mypy app"):
        assert index_of(needle) < suite, f"{needle} debe correr antes de la suite"


def test_test_job_collects_coverage_in_terminal_xml_and_html(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: `pytest` con `--cov=app` y reportes en terminal, XML y HTML."""
    coverage_runs = [run for run in _runs(jobs["test"]) if "--cov=app" in run]

    assert len(coverage_runs) == 1
    command = coverage_runs[0]
    assert "pytest" in command
    for report in ("--cov-report=term", "--cov-report=xml", "--cov-report=html"):
        assert report in command


def test_test_job_uploads_the_html_coverage_artifact(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: el HTML de cobertura se sube como artifact del workflow."""
    uploads = [
        step for step in _steps(jobs["test"]) if "upload-artifact" in str(step.get("uses", ""))
    ]

    assert len(uploads) == 1
    assert uploads[0]["with"]["name"] == "coverage-html"
    assert str(uploads[0]["with"]["path"]).rstrip("/") == "htmlcov"


# --- Job `docker` ------------------------------------------------------------


def test_docker_job_builds_without_pushing(workflow: dict[str, Any], jobs: dict[str, Any]) -> None:
    """Criterio: `docker build` (sin push) para validar el Dockerfile."""
    job = jobs["docker"]
    text = _run_text(job)

    assert "docker build" in text
    assert "docker push" not in text
    assert "--push" not in text
    assert not any("login-action" in uses for uses in _uses(job))


# --- GitHub Pages ------------------------------------------------------------


def test_pages_build_only_runs_on_main_after_test_and_docker(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: Pages solo en push a `main`, después de que `test` y `docker` pasen."""
    job = jobs["pages-build"]

    assert str(job["if"]) == MAIN_PUSH_CONDITION
    assert set(_needs(job)) == {"test", "docker"}


def test_pages_build_assembles_site_index_openapi_and_coverage(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: `_site/` con `site/index.html`, `openapi.json` y la cobertura, y se sube."""
    job = jobs["pages-build"]
    steps = _steps(job)

    downloads = [step for step in steps if "download-artifact" in str(step.get("uses", ""))]
    assert len(downloads) == 1
    assert downloads[0]["with"]["name"] == "coverage-html"
    assert str(downloads[0]["with"]["path"]).rstrip("/") == "_site/coverage"

    text = _run_text(job)
    assert "cp site/index.html _site/index.html" in text
    assert "cp docs/openapi.json _site/openapi.json" in text

    uploads = [step for step in steps if "upload-pages-artifact" in str(step.get("uses", ""))]
    assert len(uploads) == 1
    assert str(uploads[0]["with"]["path"]).rstrip("/") == "_site"


def test_pages_deploy_uses_the_github_pages_environment_and_never_cancels(
    workflow: dict[str, Any], jobs: dict[str, Any]
) -> None:
    """Criterio: `deploy-pages`, `environment: github-pages`, `concurrency: pages` sin cancelar."""
    job = jobs["pages-deploy"]

    assert str(job["if"]) == MAIN_PUSH_CONDITION
    assert _needs(job) == ["pages-build"]
    assert job["environment"]["name"] == "github-pages"
    assert job["concurrency"] == {"group": "pages", "cancel-in-progress": False}
    assert any(uses.startswith("actions/deploy-pages@") for uses in _uses(job))


# --- Acciones pinneadas ------------------------------------------------------


def test_every_action_is_pinned_by_commit_sha(workflow: dict[str, Any]) -> None:
    """S16 SEC-1: cada acción pinneada por SHA de commit (un tag movido no cambia lo que corre)."""
    uses = _workflow_uses(workflow)

    assert uses, "el workflow no usa ninguna acción"
    for uses_value in uses:
        assert PINNED_ACTION.match(uses_value), f"acción sin pinneo por SHA: {uses_value}"


def test_every_pinned_action_documents_its_exact_version() -> None:
    """S16 SEC-1: cada `uses:` lleva la versión exacta en un comentario (`# vX.Y.Z`)."""
    lines = [
        line.strip()
        for line in WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
        if "uses:" in line
    ]

    assert lines, "el workflow no usa ninguna acción"
    for line in lines:
        assert PINNED_ACTION_LINE.search(line), f"acción sin versión en comentario: {line}"


def test_dependabot_updates_github_actions() -> None:
    """S16 SEC-1: Dependabot mantiene al día los SHA pinneados de las acciones."""
    config = _load_yaml(DEPENDABOT_PATH)
    ecosystems = {update["package-ecosystem"]: update for update in config["updates"]}

    assert config["version"] == 2
    assert "github-actions" in ecosystems
    assert ecosystems["github-actions"]["directory"] == "/"


# --- site/index.html ---------------------------------------------------------


def test_site_pins_swagger_ui_to_an_exact_version(site_html: str) -> None:
    """Criterio: Swagger UI desde CDN con versión exacta `x.y.z` pinneada."""
    versions = re.findall(r"swagger-ui-dist@([^\s\"'/]+)", site_html)

    assert len(versions) >= 2, "faltan el CSS o el bundle de swagger-ui-dist"
    for version in versions:
        assert EXACT_VERSION.match(version), f"versión de swagger-ui-dist no exacta: {version}"
    assert len(set(versions)) == 1, "el CSS y el bundle deben usar la misma versión"


def test_site_loads_swagger_ui_with_subresource_integrity(site_html: str) -> None:
    """S16 SEC-2: el CSS y el bundle del CDN llevan `integrity` sha384 y `crossorigin`."""
    assert len(re.findall(r'integrity="sha384-[A-Za-z0-9+/=]+"', site_html)) == 2
    assert site_html.count('crossorigin="anonymous"') == 2


def test_site_disables_try_it_out(site_html: str) -> None:
    """Criterio: sin "Try it out" (no hay backend público): `supportedSubmitMethods: []`."""
    assert re.search(r"supportedSubmitMethods:\s*\[\s*\]", site_html)


def test_site_uses_relative_paths_for_subpath_hosting(site_html: str) -> None:
    """Criterio: funciona servido desde `/<repo>/`: `url` y link de cobertura relativos."""
    urls = re.findall(r"url:\s*\"([^\"]+)\"", site_html)
    assert urls == ["openapi.json"]

    assert re.search(r'href="coverage/index\.html"', site_html)
    assert not re.search(r'(?:href|src)="/(?!/)', site_html), "hay rutas absolutas al host"


# --- pre-commit --------------------------------------------------------------


def test_pre_commit_has_ruff_and_ruff_format(pre_commit: dict[str, Any]) -> None:
    """Criterio: hooks de ruff (check con `--fix`) y ruff-format."""
    hooks = {hook["id"]: hook for repo in pre_commit["repos"] for hook in repo.get("hooks", [])}

    assert "ruff" in hooks
    assert "--fix" in hooks["ruff"]["args"]
    assert "ruff-format" in hooks
    assert "pre-commit install" in PRE_COMMIT_PATH.read_text(encoding="utf-8")


def test_pre_commit_ruff_rev_matches_the_locked_ruff_version(
    pre_commit: dict[str, Any],
) -> None:
    """Criterio: hooks pinneados por tag, igual a la versión de ruff de `uv.lock`."""
    lock = tomllib.loads(UV_LOCK_PATH.read_text(encoding="utf-8"))
    locked = [package["version"] for package in lock["package"] if package["name"] == "ruff"]

    assert len(locked) == 1
    revs = [repo["rev"] for repo in pre_commit["repos"] if "ruff-pre-commit" in repo["repo"]]
    assert revs == [f"v{locked[0]}"]


def test_pre_commit_is_not_a_project_dependency() -> None:
    """Criterio: `pre-commit` no se agrega como dependencia del proyecto."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    dependencies = [*data["project"]["dependencies"], *data["dependency-groups"]["dev"]]

    assert not any(dep.startswith("pre-commit") for dep in dependencies)


# --- Cobertura y Makefile ----------------------------------------------------


def test_coverage_threshold_is_a_multiple_of_five() -> None:
    """Criterio: `fail_under` en `[tool.coverage.report]`, múltiplo de 5 y con un piso de 90."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    fail_under = data["tool"]["coverage"]["report"]["fail_under"]

    assert isinstance(fail_under, int)
    assert 90 <= fail_under <= 100
    assert fail_under % 5 == 0


def test_coverage_measures_the_app_package() -> None:
    """Criterio: la cobertura se mide sobre `app` (lo que reporta el CI con `--cov=app`)."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))

    assert data["tool"]["coverage"]["run"]["source"] == ["app"]
    # Sin omisiones: la cobertura no se puede "arreglar" excluyendo código del reporte.
    assert not data["tool"]["coverage"]["run"].get("omit")
    assert "fail_under" in data["tool"]["coverage"]["report"]


def test_makefile_has_a_coverage_target() -> None:
    """Criterio: target `cov` en el Makefile con `--cov=app`."""
    text = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert re.search(r"^cov:.*\n\t.*--cov=app", text, flags=re.MULTILINE)


def test_gitignore_ignores_the_coverage_outputs() -> None:
    """Criterio: las salidas de cobertura (`htmlcov/`, `coverage.xml`, `_site/`) no se versionan."""
    ignored = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()

    assert "htmlcov/" in ignored
    assert "coverage.xml" in ignored
    assert "_site/" in ignored


# --- Compose (S7 SEC-5) ------------------------------------------------------


def test_compose_declares_an_explicit_demo_environment() -> None:
    """Criterio S7 SEC-5: `ENVIRONMENT: local` en el `app-env` que comparten los servicios."""
    compose = _load_yaml(ROOT / "docker-compose.yml")
    services = compose["services"]

    for name in ("migrate", "api", "worker"):
        assert services[name]["environment"]["ENVIRONMENT"] == "local", f"falta en {name}"
