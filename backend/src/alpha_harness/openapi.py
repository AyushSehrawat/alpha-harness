"""Print the API's OpenAPI schema, the source the frontend's generated types are built from.

    uv run python -m alpha_harness.openapi > openapi.json

Builds the app without its lifespan, so nothing opens a database or reaches BRAIN.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .config import Settings
from .main import create_app


def main() -> None:
    with tempfile.TemporaryDirectory() as data_dir:
        schema = create_app(Settings(data_dir=Path(data_dir))).openapi()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
