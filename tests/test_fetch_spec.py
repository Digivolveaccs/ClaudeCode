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

        def fake(url, headers=None):
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
        fetched.assert_called_once_with("https://api.example.com/custom/spec.json",
                                        headers=None)

    def test_nothing_found_explains_the_fallback(self):
        with self._with_fetch({}):
            with self.assertRaises(SystemExit) as ctx:
                fetch_spec._read_url("https://api.example.com")
        message = str(ctx.exception)
        self.assertIn("/swagger/v1/swagger.json", message)
        self.assertIn("SwaggerHub", message)


class SpecValidationTest(unittest.TestCase):
    """A 200 is not proof of a spec - this API 200s its own 404 page."""

    IIS_404 = ("The resource you are looking for has been removed, had its name "
               "changed, or is temporarily unavailable.")

    def _with_fetch(self, available):
        from unittest import mock

        def fake(url, headers=None):
            for path, body in available.items():
                if url.endswith(path):
                    return body
            raise SystemExit("HTTP 404")

        return mock.patch.object(fetch_spec, "_fetch", side_effect=fake)

    def test_looks_like_spec_rejects_non_specs(self):
        self.assertTrue(fetch_spec.looks_like_spec(OPENAPI))
        for bad in (None, "text", {}, {"paths": {}}, {"openapi": "3.0.1"},
                    {"paths": "not a dict", "openapi": "3.0.1"}):
            self.assertFalse(fetch_spec.looks_like_spec(bad), repr(bad))

    def test_a_200_error_page_is_not_accepted_as_a_spec(self):
        with self._with_fetch({"/swagger/v1/swagger.json": self.IIS_404}):
            with self.assertRaises(SystemExit) as ctx:
                fetch_spec._read_url("https://api.example.com")
        message = str(ctx.exception)
        self.assertIn("not JSON or YAML", message)
        self.assertIn("resource you are looking for", message)

    def test_probing_continues_past_a_200_error_page(self):
        with self._with_fetch({"/swagger/v1/swagger.json": self.IIS_404,
                               "/openapi.json": json.dumps(OPENAPI)}):
            with redirect_stdout(io.StringIO()) as out:
                text = fetch_spec._read_url("https://api.example.com")
        self.assertEqual(json.loads(text)["info"]["title"], "Inform Direct API")
        self.assertIn("/openapi.json", out.getvalue())

    def test_spec_url_is_discovered_from_swagger_ui_init(self):
        init_js = ('var configObject = {"urls":[{"url":"/swagger/v2/swagger.json",'
                   '"name":"v2"}],"deepLinking":true};')
        with self._with_fetch({"/swagger/swagger-ui-init.js": init_js,
                               "/swagger/v2/swagger.json": json.dumps(OPENAPI)}):
            with redirect_stdout(io.StringIO()) as out:
                text = fetch_spec._read_url("https://api.example.com")
        self.assertEqual(json.loads(text)["openapi"], "3.0.1")
        self.assertIn("/swagger/v2/swagger.json", out.getvalue())

    def test_spec_url_is_discovered_from_the_swagger_html(self):
        html = '<script>SwaggerUIBundle({url: "/docs/openapi.json"})</script>'
        with self._with_fetch({"/swagger/index.html": html,
                               "/docs/openapi.json": json.dumps(OPENAPI)}):
            with redirect_stdout(io.StringIO()):
                text = fetch_spec._read_url("https://api.example.com")
        self.assertEqual(json.loads(text)["openapi"], "3.0.1")

    def test_spec_urls_in_extracts_and_dedupes(self):
        found = fetch_spec._spec_urls_in(
            'a "/swagger/v1/swagger.json" b "/swagger/v1/swagger.json" '
            "c '/other/openapi.yaml' d \"/not-a-spec.txt\"")
        self.assertEqual(found, ["/swagger/v1/swagger.json", "/other/openapi.yaml"])

    def test_failure_message_lists_what_was_tried(self):
        with self._with_fetch({}):
            with self.assertRaises(SystemExit) as ctx:
                fetch_spec._read_url("https://api.example.com")
        message = str(ctx.exception)
        self.assertIn("/swagger/v1/swagger.json", message)
        self.assertIn("--header", message)
        self.assertIn("SwaggerHub", message)

    def test_headers_are_passed_through_to_every_fetch(self):
        from unittest import mock
        seen = []

        def fake(url, headers=None):
            seen.append(headers)
            if url.endswith("/openapi.json"):
                return json.dumps(OPENAPI)
            raise SystemExit("HTTP 401")

        with mock.patch.object(fetch_spec, "_fetch", side_effect=fake):
            with redirect_stdout(io.StringIO()):
                fetch_spec._read_url("https://api.example.com",
                                     headers={"X-Api-Key": "k"})
        self.assertTrue(all(h == {"X-Api-Key": "k"} for h in seen), seen)

    def test_cli_header_flag_is_parsed(self):
        from unittest import mock
        with mock.patch.object(fetch_spec, "load_spec",
                               return_value=OPENAPI) as loaded:
            with redirect_stdout(io.StringIO()):
                fetch_spec.main(["--spec", "https://x", "--header",
                                 "Authorization: Bearer t"])
        self.assertEqual(loaded.call_args.kwargs["headers"],
                         {"Authorization": "Bearer t"})

    def test_bad_header_flag_is_rejected(self):
        with self.assertRaises(SystemExit):
            fetch_spec.main(["--spec", "https://x", "--header", "nocolon"])
