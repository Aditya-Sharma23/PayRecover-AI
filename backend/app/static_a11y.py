"""Static HTML accessibility checks, used only when axe-core cannot run in the page."""

from html.parser import HTMLParser


class _StaticA11yParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.is_html = False
        self.html_lang = None
        self.title = ""
        self.in_title = False
        self.images_without_alt = 0
        self.controls: list[tuple[str | None, bool, bool]] = []
        self.labels: set[str] = set()
        self.label_depth = 0
        self.empty_links = 0
        self.open_links: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.is_html = True
            self.html_lang = attrs.get("lang")
        elif tag == "title":
            self.in_title = True
        elif tag == "label":
            self.label_depth += 1
            if attrs.get("for"):
                self.labels.add(attrs["for"])
        elif tag == "img" and "alt" not in attrs:
            self.images_without_alt += 1
        elif tag in {"input", "textarea", "select"}:
            if not (tag == "input" and attrs.get("type", "text") in {"hidden", "submit", "button", "reset", "image"}):
                named = bool(attrs.get("aria-label") or attrs.get("aria-labelledby"))
                self.controls.append((attrs.get("id"), named, self.label_depth > 0))
        elif tag == "a" and "href" in attrs:
            self.open_links.append({"text": "", "named": bool(attrs.get("aria-label") or attrs.get("title"))})

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "label" and self.label_depth:
            self.label_depth -= 1
        elif tag == "a" and self.open_links:
            link = self.open_links.pop()
            if not link["text"].strip() and not link["named"]:
                self.empty_links += 1

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.open_links and data.strip():
            self.open_links[-1]["text"] += data


def static_accessibility_scan(html: str) -> dict:
    """Five rule-of-thumb checks; far fewer than axe, and labelled as such."""
    parser = _StaticA11yParser()
    parser.feed(html or "")
    if not parser.is_html:
        return {"engine": "Static HTML checks", "applicable": False, "violations": []}
    unlabeled = sum(
        not (named or nested or (control_id and control_id in parser.labels))
        for control_id, named, nested in parser.controls
    )
    rules = [
        ("html-has-lang", "Declare the document language.", not parser.html_lang, f"lang={parser.html_lang or 'missing'}"),
        ("document-title", "Give the page a descriptive title.", not parser.title.strip(), "title present" if parser.title.strip() else "title missing"),
        ("image-alt", "Provide alt text, or empty alt for decorative images.", parser.images_without_alt > 0, f"{parser.images_without_alt} image(s) without alt"),
        ("label", "Give each form control a label or accessible name.", unlabeled > 0, f"{unlabeled} of {len(parser.controls)} control(s) may lack a label"),
        ("link-name", "Give every link a readable name.", parser.empty_links > 0, f"{parser.empty_links} link(s) without a name"),
    ]
    violations = [
        {"id": rule_id, "impact": "moderate", "help": help_text, "description": detail, "nodes": 1, "targets": []}
        for rule_id, help_text, failed, detail in rules if failed
    ]
    return {
        "engine": "Static HTML checks",
        "applicable": True,
        "violations": violations,
        "rules_passed": len(rules) - len(violations),
    }
