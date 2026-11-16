"""Exporta el contrato OpenAPI: `python -m app.scripts.export_openapi`.

Escribe `docs/openapi.json` de forma determinista (claves ordenadas, UTF-8, salto final)
para que el contrato quede versionado y `tests/test_openapi_contract.py` lo verifique.
"""

import json
from pathlib import Path

from app.main import create_app

OPENAPI_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"


def render_openapi() -> str:
    """Serializa el contrato OpenAPI de la app de forma determinista."""
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    """Escribe el contrato en `docs/openapi.json`."""
    OPENAPI_PATH.write_text(render_openapi(), encoding="utf-8")


if __name__ == "__main__":
    main()
