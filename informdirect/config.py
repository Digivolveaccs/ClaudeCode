"""Settings loading for the Inform Direct client.

Precedence (highest first):
  1. keyword arguments passed to Settings.load()
  2. environment variables (INFORMDIRECT_*)
  3. a JSON config file (--config, $INFORMDIRECT_CONFIG, ./config/settings.json,
     ~/.informdirect/config.json)
  4. built-in defaults

Secrets are only ever read, never written back out or logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from .errors import ConfigError

ENV_PREFIX = "INFORMDIRECT_"

DEFAULT_CONFIG_PATHS = (
    Path("config/settings.json"),
    Path.home() / ".informdirect" / "config.json",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # --- connection -------------------------------------------------------
    base_url: str = ""
    """Root of the API, e.g. https://api.informdirect.co.uk/v1 (no trailing slash)."""

    token_url: str = ""
    """OAuth2 token endpoint. Required when auth_mode == 'oauth2'."""

    # --- credentials ------------------------------------------------------
    auth_mode: str = "api_key_token"
    """How to authenticate. Inform Direct's documented flow is 'api_key_token'.

    api_key_token - POST the API key to the auth endpoint for a short-lived
                    access token plus a refresh token (the documented flow: the
                    access token lasts 15 minutes, and /refresh is used when a
                    request comes back 401)
    api_key       - send a static key in a header on every request
    oauth2        - RFC 6749 client-credentials grant
    """

    # api_key_token settings. Paths are appended to base_url unless the *_url
    # forms are set outright.
    auth_path: str = "/authenticate"
    refresh_path: str = "/refresh"
    auth_url: str = ""
    refresh_url: str = ""
    api_key_field: str = "apiKey"
    """JSON field the API key is sent under when authenticating."""

    refresh_token_field: str = "refreshToken"
    """JSON field the refresh token is sent under, and read back from."""

    api_key_in: str = "auto"
    """Where the API key goes on the auth request: 'body', 'header' or 'auto'."""

    access_token_ttl: float = 900.0
    """Fallback token lifetime when the response does not say. Docs: 15 minutes."""

    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    audience: str = ""
    token_auth_style: str = "auto"
    """Where client credentials go on the token request: 'basic', 'body' or 'auto'.

    'auto' tries HTTP Basic first and falls back to form-body credentials if the
    token endpoint rejects it. Set explicitly once you know which one works.
    """

    api_key: str = ""
    """Sandbox or production key from Inform Direct."""

    api_key_header: str = "X-Api-Key"
    """Header name used by auth_mode 'api_key', and by api_key_in 'header'."""

    # --- behaviour --------------------------------------------------------
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    page_size: int = 100
    user_agent: str = "digivolve-informdirect/1.0"

    # --- paths ------------------------------------------------------------
    endpoints_path: str = str(REPO_ROOT / "config" / "endpoints.json")
    token_cache_path: str = str(Path.home() / ".informdirect" / "token-cache.json")
    cache_tokens: bool = True

    # provenance, for diagnostics only
    sources: list = field(default_factory=list)

    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, config_path=None, **overrides):
        data = {}
        sources = []

        path = _resolve_config_path(config_path)
        if path is not None:
            try:
                file_data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
            if not isinstance(file_data, dict):
                raise ConfigError(f"{path} must contain a JSON object")
            data.update({k: v for k, v in file_data.items() if not k.startswith("_")})
            sources.append(str(path))

        env_data = _from_env()
        if env_data:
            data.update(env_data)
            sources.append("environment")

        clean_overrides = {k: v for k, v in overrides.items() if v is not None}
        if clean_overrides:
            data.update(clean_overrides)
            sources.append("arguments")

        known = {f.name for f in fields(cls)} - {"sources"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                "Unknown setting(s): " + ", ".join(unknown) + ". Known settings: "
                + ", ".join(sorted(known))
            )

        settings = cls(**data)
        settings.sources = sources
        settings.normalise()
        return settings

    def normalise(self):
        self.base_url = (self.base_url or "").rstrip("/")
        self.token_url = (self.token_url or "").strip()
        self.auth_mode = (self.auth_mode or "oauth2").strip().lower()
        self.token_auth_style = (self.token_auth_style or "auto").strip().lower()
        self.api_key_in = (self.api_key_in or "auto").strip().lower()
        self.auth_url = (self.auth_url or "").strip()
        self.refresh_url = (self.refresh_url or "").strip()
        self.timeout = float(self.timeout)
        self.max_retries = int(self.max_retries)
        self.page_size = int(self.page_size)
        return self

    def validate(self):
        """Raise ConfigError if the settings cannot produce a working client."""
        if not self.base_url:
            raise ConfigError(
                "base_url is not set. Put the API root in config/settings.json or "
                f"set {ENV_PREFIX}BASE_URL."
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url must be an absolute URL, got {self.base_url!r}")

        if self.auth_mode == "api_key_token":
            if not self.api_key:
                raise ConfigError(
                    "auth_mode is 'api_key_token' but api_key is unset. Create a "
                    "sandbox key in Inform Direct, then set "
                    f"{ENV_PREFIX}API_KEY or api_key in config/settings.json."
                )
            if not (self.auth_url or self.auth_path):
                raise ConfigError(
                    "api_key_token needs either auth_url or auth_path (default "
                    "'/authenticate')."
                )
            if self.api_key_in not in ("body", "header", "auto"):
                raise ConfigError(
                    "api_key_in must be 'body', 'header' or 'auto', got "
                    f"{self.api_key_in!r}"
                )
        elif self.auth_mode == "oauth2":
            missing = [
                name
                for name in ("token_url", "client_id", "client_secret")
                if not getattr(self, name)
            ]
            if missing:
                raise ConfigError(
                    "auth_mode is 'oauth2' but these are unset: "
                    + ", ".join(missing)
                    + ". Set them via "
                    + ", ".join(ENV_PREFIX + m.upper() for m in missing)
                    + " or config/settings.json."
                )
            if self.token_auth_style not in ("basic", "body", "auto"):
                raise ConfigError(
                    "token_auth_style must be 'basic', 'body' or 'auto', got "
                    f"{self.token_auth_style!r}"
                )
        elif self.auth_mode == "api_key":
            if not self.api_key:
                raise ConfigError(
                    "auth_mode is 'api_key' but api_key is unset "
                    f"(set {ENV_PREFIX}API_KEY)."
                )
            if not self.api_key_header:
                raise ConfigError("api_key_header must not be empty")
        else:
            raise ConfigError(
                "auth_mode must be 'api_key_token', 'api_key' or 'oauth2', got "
                f"{self.auth_mode!r}"
            )
        return self

    def resolved_auth_url(self):
        return self.auth_url or (self.base_url + self.auth_path)

    def resolved_refresh_url(self):
        return self.refresh_url or (self.base_url + self.refresh_path)

    def redacted(self):
        """A dict safe to print or log - secrets replaced with a mask."""
        secret_names = {"client_secret", "api_key"}
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in secret_names and value:
                value = f"<set, {len(str(value))} chars>"
            out[f.name] = value
        return out


def _resolve_config_path(explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path

    env_path = os.environ.get(ENV_PREFIX + "CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"{ENV_PREFIX}CONFIG points at a file that does not exist: {path}"
            )
        return path

    for candidate in DEFAULT_CONFIG_PATHS:
        path = candidate.expanduser()
        if path.is_file():
            return path
        repo_candidate = (REPO_ROOT / candidate).expanduser()
        if repo_candidate.is_file():
            return repo_candidate
    return None


_BOOL_FIELDS = {"cache_tokens"}
_FLOAT_FIELDS = {"timeout", "backoff_base", "backoff_cap",
                 "access_token_ttl"}
_INT_FIELDS = {"max_retries", "page_size"}


def _from_env(environ=None):
    environ = os.environ if environ is None else environ
    known = {f.name for f in fields(Settings)} - {"sources"}
    out = {}
    for name in known:
        raw = environ.get(ENV_PREFIX + name.upper())
        if raw is None or raw == "":
            continue
        out[name] = _coerce(name, raw)
    return out


def _coerce(name, raw):
    if name in _BOOL_FIELDS:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if name in _FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{name.upper()} must be a number") from exc
    if name in _INT_FIELDS:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{name.upper()} must be an integer") from exc
    return raw
