import unittest

from app.report_html import render_report_html


class ReportHtmlTests(unittest.TestCase):
    def test_report_values_are_escaped(self):
        rendered = render_report_html({
            "overall_status": "failed",
            "target_url": "https://example.com/",
            "summary": {"failed": 1},
            "checks": [{
                "check_id": "h1",
                "status": "failed",
                "message": "<script>alert(1)</script>",
                "evidence": {"text": "<unsafe>"},
            }],
        })
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("&lt;unsafe&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
