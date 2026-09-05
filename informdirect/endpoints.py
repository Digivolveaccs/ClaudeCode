"""Endpoint map.

Nothing else in the package hardcodes a URL path. Operations are named
(`list_companies`, `get_company`, ...) and resolved through config/endpoints.json,
so correcting that one file is enough to point the client at the real API.
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, EndpointNotConfigured

DEFAULT_PAGINATION = {
    "style": "auto",
    "page_param": "page",
    "page_size_param": "pageSize",
    "offset_param": "offset",
    "limit_param": "limit",
    "cursor_param": "cursor",
    "first_page": 1,
    "max_pages": 500,
}


@dataclass
class Operation:
    name: str
    method: str
    path: str
    description: str = ""
    pagination: dict = field(default_factory=dict)

    def render(self, **params):
        """Substitute {placeholders} in the path, URL-quoting each value."""
        from urllib.parse import quote

        required = _placeholders(self.path)
        missing = sorted(required - set(params))
        if missing:
            raise ConfigError(
                f"operation {self.name!r} needs path parameter(s): "
                + ", ".join(missing)
            )
        safe = {k: quote(str(v), safe="") for k, v in params.items() if k in required}
        return self.method.upper(), self.path.format(**safe)


class EndpointMap:
    def __init__(self, operations, pagination=None, status="unknown", source=None):
        self._operations = operations
        self.pagination = {**DEFAULT_PAGINATION, **(pagination or {})}
        self.status = status
        self.source = source

    @property
    def is_provisional(self):
        return self.status == "provisional"

    def names(self):
        return sorted(self._operations)

    def has(self, name):
        return name in self._operations

    def get(self, name):
        try:
            return self._operations[name]
        except KeyError:
            raise EndpointNotConfigured(
                f"no endpoint configured for {name!r}. Known operations: "
                + ", ".join(self.names())
                + f". Add it to {self.source or 'config/endpoints.json'} or "
                "regenerate with scripts/fetch_spec.py."
            ) from None

    def resolve(self, name, **params):
        return self.get(name).render(**params)

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"endpoint map not found at {path}. Copy config/endpoints.json into "
                "place or run scripts/fetch_spec.py --write."
            )
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data, source=str(path))

    @classmethod
    def from_dict(cls, data, source=None):
        if not isinstance(data, dict):
            raise ConfigError("endpoint map must be a JSON object")
        raw_ops = data.get("operations")
        if not isinstance(raw_ops, dict) or not raw_ops:
            raise ConfigError("endpoint map has no 'operations' object")

        operations = {}
        for name, spec in raw_ops.items():
            if not isinstance(spec, dict):
                raise ConfigError(f"operation {name!r} must be an object")
            method = spec.get("method")
            op_path = spec.get("path")
            if not method or not op_path:
                raise ConfigError(f"operation {name!r} needs both 'method' and 'path'")
            if not str(op_path).startswith("/"):
                raise ConfigError(
                    f"operation {name!r} path must start with '/', got {op_path!r}"
                )
            operations[name] = Operation(
                name=name,
                method=str(method).upper(),
                path=str(op_path),
                description=spec.get("description", ""),
                pagination=spec.get("pagination") or {},
            )

        return cls(
            operations,
            pagination=data.get("pagination"),
            status=data.get("_status", "unknown"),
            source=source,
        )


def _placeholders(template):
    return {
        name
        for _, name, _, _ in string.Formatter().parse(template)
        if name
    }
