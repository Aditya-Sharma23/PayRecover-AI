"""AI writes the fix: edits must match, stay safe, and (for a demo store) be shown to close the hole."""

import isolation
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import fixer
from app.main import app
from app.schemas import FixEdit, FixRequest

STORE = Path(__file__).resolve().parents[2] / "vuln-store" / "store_app.py"
XSS_LINE = 'results = f"<p id=\\"search-results\\">You searched for: {q}</p>" if q else ""'
XSS_FIXED = 'results = f"<p id=\\"search-results\\">You searched for: {html.escape(q)}</p>" if q else ""'


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class EditTests(unittest.TestCase):
    def test_each_edit_must_match_exactly_once(self):
        self.assertEqual(fixer.apply_edits("a = 1\nb = 2\n", [FixEdit(find="b = 2", replace="b = 3")]), "a = 1\nb = 3\n")
        with self.assertRaises(fixer.FixRejected):
            fixer.apply_edits("x\nx\n", [FixEdit(find="x", replace="y")])
        with self.assertRaises(fixer.FixRejected):
            fixer.apply_edits("x\n", [FixEdit(find="missing", replace="y")])
        with self.assertRaises(fixer.FixRejected):
            fixer.apply_edits("x\n", [])

    def test_a_fix_may_not_bring_in_anything_that_runs_programs_or_opens_files(self):
        original = "import json\nvalue = json.dumps(1)\n"
        self.assertEqual(fixer.safety_problems(original, "import html\nimport json\nvalue = html.escape(json.dumps(1))\n"), [])
        self.assertEqual(fixer.safety_problems(original, "import json, os\nos.system('x')\n"), ["uses system", "imports os"])
        self.assertIn("uses eval", fixer.safety_problems(original, "import json\nvalue = eval('1')\n"))


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.assertIn(XSS_LINE, STORE.read_text(encoding="utf-8"), "the demo store's search line changed")

    def propose(self, edits, issue="xss"):
        async def ask(_prompt):
            return {"summary": "Escape the search text.", "edits": edits}

        with patch.object(fixer.ai, "ask_json", ask):
            return asyncio.run(fixer.propose(FixRequest(issue=issue, store="vulnerable")))

    def test_a_real_fix_is_run_and_shown_to_close_the_hole(self):
        fix = self.propose([{"find": XSS_LINE, "replace": XSS_FIXED}, {"find": "import json\n", "replace": "import html\nimport json\n"}])
        self.assertTrue(fix.verified)
        checks = {check.name: check for check in fix.checks}
        self.assertEqual(checks["bug_reproduced"].status, "passed")
        self.assertIn("escaped", checks["fix_works"].message)
        self.assertIn("+    results = f", fix.diff)

    def test_a_fix_that_does_not_close_the_hole_is_not_verified(self):
        fix = self.propose([{"find": XSS_LINE, "replace": XSS_LINE + "  # looked at"}])
        self.assertFalse(fix.verified)
        self.assertEqual(next(c for c in fix.checks if c.name == "fix_works").status, "failed")

    def test_a_failed_fix_goes_back_to_the_ai_with_the_reason(self):
        answers = [
            {"summary": "Escape it.", "edits": [{"find": XSS_LINE, "replace": XSS_FIXED}]},  # forgets import html
            {"summary": "Escape it.", "edits": [{"find": XSS_LINE, "replace": XSS_FIXED},
                                                {"find": "import json\n", "replace": "import html\nimport json\n"}]},
        ]
        prompts = []

        async def ask(prompt):
            prompts.append(prompt)
            return answers[len(prompts) - 1]

        with patch.object(fixer.ai, "ask_json", ask):
            fix = asyncio.run(fixer.propose(FixRequest(issue="xss", store="vulnerable")))
        self.assertTrue(fix.verified)
        self.assertIn("NameError", prompts[1])
        self.assertEqual(fix.checks[0].name, "attempt_1")

    def test_a_pasted_store_is_checked_but_never_run(self):
        async def ask(_prompt):
            return {"summary": "Escape it.", "edits": [{"find": "return q", "replace": "return html.escape(q)"},
                                                       {"find": "def page", "replace": "import html\n\n\ndef page"}]}

        with patch.object(fixer.ai, "ask_json", ask):
            fix = asyncio.run(fixer.propose(FixRequest(issue="xss", store="connected", code="def page(q):\n    return q\n",
                                                       filename="shop.py")))
        self.assertFalse(fix.verified)
        self.assertEqual(next(c for c in fix.checks if c.name == "fix_works").status, "skipped")

    def test_an_unsafe_fix_is_refused_over_http(self):
        async def ask(_prompt):
            return {"summary": "x", "edits": [{"find": XSS_LINE, "replace": XSS_LINE + "; eval('1')"}]}

        with patch.object(fixer.ai, "ask_json", ask):
            response = TestClient(app).post("/api/fixes", json={"issue": "xss", "store": "vulnerable"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("eval", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
