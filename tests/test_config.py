import json
import os
import tempfile
import unittest
from pathlib import Path

from informdirect.config import Settings, _from_env
from informdirect.errors import ConfigError


class SettingsTest(unittest.TestCase):
    def test_env_beats_file_and_args_beat_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "base_url": "https://from-file/v1",
                "auth_mode": "api_key",
                "api_key": "file-key",
            }))
            env = {"INFORMDIRECT_BASE_URL": "https://from-env/v1"}
            with _patched_env(env):
                settings = Settings.load(config_path=path)
                self.assertEqual(settings.base_url, "https://from-env/v1")
                settings = Settings.load(config_path=path, base_url="https://arg/v1")
                self.assertEqual(settings.base_url, "https://arg/v1")

    def test_trailing_slash_is_stripped(self):
        s = Settings.load(base_url="https://api.example.com/v1/", auth_mode="api_key",
                          api_key="k")
        self.assertEqual(s.base_url, "https://api.example.com/v1")

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            Settings.load(basurl="https://x")
        self.assertIn("basurl", str(ctx.exception))

    def test_validate_lists_every_missing_oauth_field(self):
        s = Settings.load(base_url="https://api.example.com")
        with self.assertRaises(ConfigError) as ctx:
            s.validate()
        message = str(ctx.exception)
        for name in ("token_url", "client_id", "client_secret"):
            self.assertIn(name, message)

    def test_validate_rejects_relative_base_url(self):
        s = Settings.load(base_url="api.example.com", auth_mode="api_key", api_key="k")
        with self.assertRaises(ConfigError):
            s.validate()

    def test_validate_rejects_unknown_auth_mode(self):
        s = Settings.load(base_url="https://x", auth_mode="magic")
        with self.assertRaises(ConfigError):
            s.validate()

    def test_api_key_mode_needs_a_key(self):
        s = Settings.load(base_url="https://x", auth_mode="api_key")
        with self.assertRaises(ConfigError):
            s.validate()

    def test_redacted_hides_secrets(self):
        s = Settings.load(base_url="https://x", auth_mode="api_key",
                          api_key="super-secret", client_secret="also-secret")
        redacted = s.redacted()
        self.assertNotIn("super-secret", json.dumps(redacted))
        self.assertNotIn("also-secret", json.dumps(redacted))
        self.assertEqual(redacted["base_url"], "https://x")

    def test_env_coercion(self):
        env = {
            "INFORMDIRECT_MAX_RETRIES": "7",
            "INFORMDIRECT_TIMEOUT": "12.5",
            "INFORMDIRECT_CACHE_TOKENS": "false",
        }
        parsed = _from_env(env)
        self.assertEqual(parsed["max_retries"], 7)
        self.assertEqual(parsed["timeout"], 12.5)
        self.assertIs(parsed["cache_tokens"], False)

    def test_env_coercion_rejects_nonsense(self):
        with self.assertRaises(ConfigError):
            _from_env({"INFORMDIRECT_MAX_RETRIES": "lots"})


class _patched_env:
    def __init__(self, env):
        self.env = env
        self.saved = {}

    def __enter__(self):
        for key, value in self.env.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
