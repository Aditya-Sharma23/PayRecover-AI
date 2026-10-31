import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import ai
from app.ai import (
    AIConfigurationError,
    AIProviderError,
    _answer_text,
    _extract_json,
    _settings,
    create_plan,
)
from app.schemas import CheckName, AIPlanRequest


class AITests(unittest.TestCase):
    def test_missing_key_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AIConfigurationError):
                _settings()

    def test_settings_trims_quotes_and_whitespace_without_exposing_key(self):
        with patch.dict(
            os.environ,
            {"TESTPILOT_GOOGLE_API_KEY": "  'local-key'  ", "TESTPILOT_AI_MODEL": " gemini-3.8-flash "},
            clear=True,
        ):
            self.assertEqual(_settings(), ("local-key", "gemini-3.8-flash"))

    def test_json_extraction_accepts_markdown_fence(self):
        self.assertEqual(_extract_json('```json\n{"ok": true}\n```'), {"ok": True})

    def test_invalid_ai_json_is_explicit(self):
        with self.assertRaises(AIProviderError):
            _extract_json("not json")

    def test_plan_validates_structured_model_output(self):
        request = AIPlanRequest(
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

        with patch("app.ai._generate", fake_generate):
            plan = asyncio.run(create_plan(request))
        self.assertEqual(plan.checks, [CheckName.PAGE_TITLE])

    def test_answer_text_skips_thought_parts(self):
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
        request = AIPlanRequest(goal="Check the API", test_type="api", authorized=True)

        async def fake_generate(_prompt: str) -> str:
            return (
                '{"goal":"Check the API","test_type":"api","checks":["page_title"],'
                '"expectations":["A title"],"limitations":[],"rationale":"r"}'
            )

        with patch("app.ai._generate", fake_generate):
            with self.assertRaises(AIProviderError):
                asyncio.run(create_plan(request))



class ProviderRetryTests(unittest.TestCase):
    ENV = {"TESTPILOT_GOOGLE_API_KEY": "test-key", "TESTPILOT_AI_MODEL": "gemini-main"}

    def run_with(self, outcomes, env=None):
        """outcomes: model name -> list of results to return in order ("busy", "fatal" or answer text)."""
        calls = []

        async def fake_google(_prompt, _key, model, _timeout):
            calls.append(model)
            result = outcomes[model].pop(0)
            if result == "busy":
                raise ai._RetryableProviderError(f"{model} returned HTTP 503")
            if result == "stall":
                raise ai._StalledProviderError(f"{model} did not answer in time")
            if result == "fatal":
                raise AIProviderError(f"{model} returned HTTP 400")
            return result

        with (
            patch.dict(os.environ, {**self.ENV, **(env or {})}, clear=True),
            patch("app.ai._generate_google", fake_google),
            patch("app.ai.RETRY_DELAY_SECONDS", 0),
        ):
            try:
                return asyncio.run(ai._generate("prompt")), calls
            except AIProviderError as exc:
                return exc, calls

    def test_busy_model_is_retried_once(self):
        result, calls = self.run_with({"gemini-main": ["busy", "answer"]})
        self.assertEqual(result, "answer")
        self.assertEqual(calls, ["gemini-main", "gemini-main"])

    def test_fallback_models_are_tried_in_order(self):
        result, calls = self.run_with(
            {"gemini-main": ["busy", "busy"], "gemini-b": ["busy"], "gemini-c": ["answer"]},
            {"TESTPILOT_AI_FALLBACK_MODELS": "gemini-b, gemini-main, gemini-c"},
        )
        self.assertEqual(result, "answer")
        self.assertEqual(calls, ["gemini-main", "gemini-main", "gemini-b", "gemini-c"])

    def test_a_stalled_model_is_skipped_for_the_fallback(self):
        result, calls = self.run_with(
            {"gemini-main": ["stall"], "gemini-b": ["answer"]}, {"TESTPILOT_AI_FALLBACK_MODELS": "gemini-b"}
        )
        self.assertEqual(result, "answer")
        self.assertEqual(calls, ["gemini-main", "gemini-b"])

    def test_the_answer_is_labelled_with_the_model_that_replied(self):
        async def ask():
            await ai._generate("prompt")
            return ai.answer_source()

        async def fake_google(_prompt, _key, model, _timeout):
            if model == "gemini-main":
                raise ai._StalledProviderError("stalled")
            return "answer"

        env = {**self.ENV, "TESTPILOT_AI_FALLBACK_MODELS": "gemini-3.7-flash"}
        with patch.dict(os.environ, env, clear=True), patch("app.ai._generate_google", fake_google):
            self.assertEqual(asyncio.run(ask()), "Gemini (gemini-3.7-flash)")

    def test_a_bad_request_is_not_retried(self):
        result, calls = self.run_with({"gemini-main": ["fatal"]}, {"TESTPILOT_AI_FALLBACK_MODELS": "gemini-b"})
        self.assertIsInstance(result, AIProviderError)
        self.assertEqual(calls, ["gemini-main"])

    def test_every_attempt_busy_reports_the_last_failure(self):
        result, calls = self.run_with({"gemini-main": ["busy", "busy"]})
        self.assertIsInstance(result, AIProviderError)
        self.assertIn("busy or unreachable after 2 attempt(s)", str(result))



class AiNamingTests(unittest.TestCase):
    def test_family_names(self):
        self.assertEqual(ai.ai_name("gemini-3.8-flash"), "Gemini")
        self.assertEqual(ai.ai_name("llama3"), "AI")

    def test_the_model_comes_from_the_ai_setting_with_a_gemini_default(self):
        with patch.dict(os.environ, {"TESTPILOT_AI_MODEL": "gemini-3.5-flash-lite"}, clear=True):
            status = ai.configuration_status()
        self.assertEqual((status["model"], status["name"]), ("gemini-3.5-flash-lite", "Gemini"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ai.configuration_status()["model"], "gemini-3.8-flash")



class AnswerLimitTests(unittest.TestCase):
    def test_long_answers_are_trimmed_not_rejected(self):
        answer = {
            "summary": "x" * 2500,
            "findings": "one finding as a plain string",
            "limitations": [f"note {n}" for n in range(14)] + ["", "  "],
            "next_steps": ["fix it"],
            "risk_level": "review",
        }

        async def fake_generate(_prompt):
            return json.dumps(answer)

        with patch("app.ai._generate", fake_generate):
            explanation = asyncio.run(ai.explain_report({"checks": []}))
        self.assertEqual(len(explanation.summary), ai.MAX_SUMMARY_CHARS)
        self.assertEqual(explanation.findings, ["one finding as a plain string"])
        self.assertEqual(explanation.limitations, [f"note {n}" for n in range(10)])
        self.assertEqual(explanation.risk_level, "REVIEW")

    def test_error_messages_never_hard_code_a_model_family(self):
        # Messages say "the AI model" or name the configured model, so they stay right when the model changes.
        source = (Path(ai.__file__)).read_text(encoding="utf-8")
        messages = [line for line in source.splitlines()
                    if "AIProviderError(" in line and "Gemini " in line.split("(", 1)[1]]
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
