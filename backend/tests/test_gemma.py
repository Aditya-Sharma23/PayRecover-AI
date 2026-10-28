import asyncio
import os
import unittest
from unittest.mock import patch

from app.gemma import (
    GemmaConfigurationError,
    GemmaProviderError,
    _answer_text,
    _extract_json,
    _settings,
    create_plan,
)
from app.schemas import CheckName, GemmaPlanRequest


class GemmaTests(unittest.TestCase):
    def test_missing_key_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(GemmaConfigurationError):
                _settings()

    def test_settings_trims_quotes_and_whitespace_without_exposing_key(self):
        with patch.dict(
            os.environ,
            {"TESTPILOT_GOOGLE_API_KEY": "  'local-key'  ", "TESTPILOT_GEMMA_MODEL": " gemma-4-26b-a4b-it "},
            clear=True,
        ):
            self.assertEqual(_settings(), ("local-key", "gemma-4-26b-a4b-it"))

    def test_json_extraction_accepts_markdown_fence(self):
        self.assertEqual(_extract_json('```json\n{"ok": true}\n```'), {"ok": True})

    def test_invalid_gemma_json_is_explicit(self):
        with self.assertRaises(GemmaProviderError):
            _extract_json("not json")

    def test_plan_validates_structured_model_output(self):
        request = GemmaPlanRequest(
            goal="Check that the landing page has a title",
            target_url="https://example.com/",
            authorized=True,
        )

        async def fake_generate(_prompt: str) -> str:
            return (
                '{"goal":"Check that the landing page has a title",'
                '"test_type":"website","checks":["page_title"],'
                '"expectations":["A non-empty title is observed"],'
                '"limitations":[],"rationale":"The goal names the title condition."}'
            )

        with patch("app.gemma._generate", fake_generate):
            plan = asyncio.run(create_plan(request))
        self.assertEqual(plan.checks, [CheckName.PAGE_TITLE])

    def test_answer_text_skips_gemma_4_thought_parts(self):
        data = {"candidates": [{"content": {"parts": [
            {"text": "* reasoning about the request", "thought": True},
            {"text": "`", "thought": True},
            {"text": '{"ok": true}'},
        ]}}]}
        self.assertEqual(_extract_json(_answer_text(data)), {"ok": True})

    def test_answer_text_without_answer_is_rejected(self):
        with self.assertRaises(KeyError):
            _answer_text({"candidates": [{"content": {"parts": [{"text": "x", "thought": True}]}}]})

    def test_plan_with_checks_from_another_test_type_is_rejected(self):
        request = GemmaPlanRequest(goal="Check the API", test_type="api", authorized=True)

        async def fake_generate(_prompt: str) -> str:
            return (
                '{"goal":"Check the API","test_type":"api","checks":["page_title"],'
                '"expectations":["A title"],"limitations":[],"rationale":"r"}'
            )

        with patch("app.gemma._generate", fake_generate):
            with self.assertRaises(GemmaProviderError):
                asyncio.run(create_plan(request))


if __name__ == "__main__":
    unittest.main()
