import json
import unittest

from app.openapi_import import OpenApiError, import_openapi
from app.schemas import ApiRunRequest, OpenApiImportRequest

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "2.1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets",
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}},
            },
            "post": {"operationId": "createPet", "responses": {"201": {"description": "created"}}},
        },
        "/pets/{petId}": {"get": {"operationId": "getPet", "responses": {"200": {"description": "ok"}}}},
        "/search": {"get": {
            "parameters": [{"name": "q", "in": "query", "required": True}],
            "responses": {"200": {"description": "ok"}},
        }},
    },
    "components": {"schemas": {"Pet": {
        "type": "object", "required": ["id"],
        "properties": {"id": {"type": "integer"}, "tag": {"type": "string", "nullable": True},
                       "parent": {"$ref": "#/components/schemas/Pet"}},
    }}},
}


def operations(spec, **options):
    result = import_openapi(OpenApiImportRequest(spec=spec if isinstance(spec, str) else json.dumps(spec), **options))
    return result, {op.operation_id: op for op in result.operations}


class OpenApiImportTests(unittest.TestCase):
    def test_get_operations_become_runnable_suggestions(self):
        result, ops = operations(SPEC)
        self.assertEqual(result.base_url, "https://api.example.com/v1")
        pets = ops["listPets"]
        self.assertTrue(pets.runnable)
        self.assertEqual(pets.url, "https://api.example.com/v1/pets")
        self.assertEqual(pets.expected_status_code, 200)
        self.assertEqual(pets.expected_content_type, "application/json")
        item = pets.response_schema["items"]
        self.assertEqual(item["properties"]["tag"]["type"], ["string", "null"])
        # A self-reference is cut instead of expanding forever.
        self.assertEqual(item["properties"]["parent"], {})
        # The suggestion is accepted by the real request model.
        ApiRunRequest(url=pets.url, authorization_confirmed=True, checks=["api_json_schema"],
                      response_schema=pets.response_schema)

    def test_unsafe_or_incomplete_operations_are_explained_not_run(self):
        _, ops = operations(SPEC)
        self.assertFalse(ops["createPet"].runnable)
        self.assertIn("Only GET", ops["createPet"].reason)
        self.assertIn("path parameter", ops["getPet"].reason)
        self.assertIn("q", ops["GET /search"].reason)

    def test_yaml_and_supplied_base_url(self):
        yaml_spec = "swagger: '2.0'\ninfo: {title: Old, version: '1'}\nhost: old.example.com\npaths:\n  /status:\n    get:\n      produces: [application/json]\n      responses:\n        '200': {description: ok, schema: {type: object}}\n"
        result, ops = operations(yaml_spec)
        self.assertEqual(ops["GET /status"].url, "https://old.example.com/status")
        _, ops = operations(SPEC, base_url="https://staging.example.com/v1")
        self.assertEqual(ops["listPets"].url, "https://staging.example.com/v1/pets")

    def test_documents_that_are_not_specs_or_use_yaml_aliases_are_rejected(self):
        for text in ('{"hello": 1}', "a: &x [1]\nb: *x\nopenapi: 3.0.0\npaths: {}", "::: not yaml :::\n\t-"):
            with self.assertRaises(OpenApiError):
                operations(text)

    def test_reference_fan_out_is_bounded(self):
        schemas = {f"S{i}": {"type": "object", "properties": {f"p{j}": {"$ref": f"#/components/schemas/S{i + 1}"} for j in range(30)}} for i in range(8)}
        spec = {"openapi": "3.1.0", "info": {}, "servers": [{"url": "https://x.example.com"}], "components": {"schemas": schemas},
                "paths": {"/a": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/S0"}}}}}}}}}
        with self.assertRaises(OpenApiError):
            operations(spec)


if __name__ == "__main__":
    unittest.main()
