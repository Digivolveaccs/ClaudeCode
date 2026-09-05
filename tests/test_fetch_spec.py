import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fetch_spec  # noqa: E402

OPENAPI = {
    "openapi": "3.0.1",
    "info": {"title": "Inform Direct API", "version": "1.0"},
    "servers": [{"url": "https://{env}.informdirect.example/v1",
                 "variables": {"env": {"default": "api"}}}],
    "paths": {
        "/portfolio/companies": {
            "get": {"operationId": "listCompanies", "summary": "List companies"}
        },
        "/portfolio/companies/{companyKey}": {
            "get": {"operationId": "getCompanyById", "summary": "Get one company"}
        },
        "/portfolio/companies/{companyKey}/officers": {
            "get": {"operationId": "listCompanyOfficers", "summary": "Officers"}
        },
        "/portfolio/companies/{companyKey}/shareholdings": {
            "get": {"operationId": "getShareholdings", "summary": "Shareholdings"}
        },
        "/health": {"get": {"operationId": "health"}},
    },
}

SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Legacy", "version": "2"},
    "host": "legacy.example.com",
    "basePath": "/api",
    "schemes": ["https"],
    "paths": {"/companies": {"get": {"summary": "all companies"}}},
}


class SpecImportTest(unittest.TestCase):
    def test_server_base_resolves_variables(self):
        self.assertEqual(fetch_spec.server_base(OPENAPI),
                         "https://api.informdirect.example/v1")

    def test_swagger2_base_is_assembled_from_host_and_basepath(self):
        self.assertEqual(fetch_spec.server_base(SWAGGER2),
                         "https://legacy.example.com/api")

    def test_operations_map_and_path_param_is_renamed(self):
        mapped, all_ops, unmatched = fetch_spec.build_map(OPENAPI, {})
        self.assertEqual(mapped["list_companies"]["path"], "/portfolio/companies")
        self.assertEqual(mapped["get_company"]["path"],
                         "/portfolio/companies/{company_id}")
        self.assertEqual(mapped["list_officers"]["path"],
                         "/portfolio/companies/{company_id}/officers")
        self.assertEqual(mapped["list_shareholders"]["path"],
                         "/portfolio/companies/{company_id}/shareholdings")
        self.assertEqual(unmatched, ["list_filings"])
        self.assertEqual(len(all_ops), 5)

    def test_matching_by_path_shape_when_operation_ids_are_absent(self):
        spec = {
            "paths": {
                "/companies": {"get": {}},
                "/companies/{id}": {"get": {}},
                "/companies/{id}/members": {"get": {}},
            }
        }
        mapped, _, _ = fetch_spec.build_map(spec, {})
        self.assertEqual(mapped["list_companies"]["path"], "/companies")
        self.assertEqual(mapped["get_company"]["path"], "/companies/{company_id}")
        self.assertEqual(mapped["list_shareholders"]["path"],
                         "/companies/{company_id}/members")

    def test_unmatched_operations_keep_the_existing_entry(self):
        existing = {"operations": {"list_filings": {"method": "GET",
                                                    "path": "/kept"}}}
        mapped, _, unmatched = fetch_spec.build_map(OPENAPI, existing)
        self.assertEqual(mapped["list_filings"]["path"], "/kept")
        self.assertEqual(unmatched, ["list_filings"])

    def test_generated_map_is_loadable_by_the_client(self):
        from informdirect.endpoints import EndpointMap

        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(OPENAPI))
            out_path = Path(tmp) / "endpoints.json"
            with redirect_stdout(io.StringIO()):
                rc = fetch_spec.main(["--spec", str(spec_path), "--out",
                                      str(out_path), "--write"])
            self.assertEqual(rc, 0)
            endpoints = EndpointMap.load(out_path)
            self.assertEqual(endpoints.status, "partial")
            self.assertEqual(
                endpoints.resolve("get_company", company_id="01234567"),
                ("GET", "/portfolio/companies/01234567"),
            )

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(OPENAPI))
            out_path = Path(tmp) / "endpoints.json"
            with redirect_stdout(io.StringIO()):
                fetch_spec.main(["--spec", str(spec_path), "--out", str(out_path)])
            self.assertFalse(out_path.exists())

    def test_non_openapi_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text(json.dumps({"hello": "world"}))
            with self.assertRaises(SystemExit):
                fetch_spec.main(["--spec", str(path)])


if __name__ == "__main__":
    unittest.main()


class SpecDiscoveryTest(unittest.TestCase):
    """`--spec https://api.example.com` should find the spec on its own."""

    def _with_fetch(self, available):
        """Patch _fetch so only the paths in `available` succeed."""
        from unittest import mock

        def fake(url):
            for path, body in available.items():
                if url.endswith(path):
                    return body
            raise SystemExit("HTTP 404")

        return mock.patch.object(fetch_spec, "_fetch", side_effect=fake)

    def test_bare_host_tries_the_common_locations(self):
        body = json.dumps(OPENAPI)
        with self._with_fetch({"/swagger/v1/swagger.json": body}):
            with redirect_stdout(io.StringIO()) as out:
                text = fetch_spec._read_url("https://api.example.com")
        self.assertEqual(json.loads(text)["info"]["title"], "Inform Direct API")
        self.assertIn("/swagger/v1/swagger.json", out.getvalue())

    def test_falls_through_to_a_later_candidate(self):
        body = json.dumps(OPENAPI)
        with self._with_fetch({"/openapi.json": body}):
            with redirect_stdout(io.StringIO()):
                text = fetch_spec._read_url("https://api.example.com/")
        self.assertEqual(json.loads(text)["openapi"], "3.0.1")

    def test_an_explicit_document_url_is_not_probed(self):
        from unittest import mock
        with mock.patch.object(fetch_spec, "_fetch",
                               return_value="{}") as fetched:
            fetch_spec._read_url("https://api.example.com/custom/spec.json")
        fetched.assert_called_once_with("https://api.example.com/custom/spec.json")

    def test_nothing_found_explains_the_fallback(self):
        with self._with_fetch({}):
            with self.assertRaises(SystemExit) as ctx:
                fetch_spec._read_url("https://api.example.com")
        message = str(ctx.exception)
        self.assertIn("/swagger/v1/swagger.json", message)
        self.assertIn("SwaggerHub", message)
