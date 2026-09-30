import json
import re

from doomtp_bot.api.app import create_app
from doomtp_bot.core.health import HealthRegistry

# The route docstrings become the descriptions in /openapi.json and /docs. Readers of those have no
# ADRs or architecture document, so references to them belong in a `#` comment above the route.
INTERNAL = re.compile(r"ADR-\d+|architecture §")


def test_the_openapi_schema_cites_no_internal_documents() -> None:
    schema = json.dumps(create_app(HealthRegistry()).openapi(), ensure_ascii=False)
    assert INTERNAL.findall(schema) == []
