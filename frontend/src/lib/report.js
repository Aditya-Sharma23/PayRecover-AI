// Pure helpers that shape a canonical report for the dashboard panels.
import { CHECK_CATEGORY, CHECK_LABELS } from "./requestBody.js";

export const CATEGORIES = [
  ["response", "HTTP response", "Status, timing, headers and body"],
  ["content", "Content and flows", "Title, heading, console, text and journeys"],
  ["security", "Security", "Headers, cookies, exposed files, active probes"],
  ["accessibility", "Accessibility", "axe-core WCAG rules and keyboard focus"],
  ["contract", "API contract", "Schemathesis on a discovered OpenAPI schema"],
];

const RANK = { error: 4, failed: 3, warning: 2, skipped: 1, passed: 0 };

export const label = (checkId) => CHECK_LABELS[checkId] || checkId;

// Worst status wins; an empty group was not run.
export function groupStatus(checks) {
  if (!checks.length) return "not_run";
  return checks.reduce((worst, check) => (RANK[check.status] > RANK[worst] ? check.status : worst), "passed");
}

export function groupChecks(report) {
  return CATEGORIES.map(([key, title, description]) => {
    const checks = report.checks.filter((check) => (CHECK_CATEGORY[check.check_id] || "content") === key);
    return { key, title, description, checks, status: groupStatus(checks) };
  });
}

// Security findings and accessibility violations, one card each, most severe first.
export function collectFindings(report) {
  const order = { critical: 0, serious: 1, high: 1, medium: 2, moderate: 2, low: 3, minor: 3, info: 4 };
  const items = [];
  for (const check of report.checks) {
    const evidence = check.evidence || {};
    for (const item of evidence.findings || []) {
      items.push({ title: item.title, severity: item.severity, detail: item.detail, fix: item.recommendation, source: label(check.check_id) });
    }
    if (check.check_id === "accessibility") {
      for (const violation of evidence.violations || []) {
        items.push({
          title: violation.help,
          severity: violation.impact || "moderate",
          detail: `${violation.nodes || 0} element(s)${violation.targets?.length ? `: ${violation.targets.join(", ")}` : ""}${violation.description ? ` · ${violation.description}` : ""}`,
          fix: String(violation.help_url || "").startsWith("https://dequeuniversity.com/") ? violation.help_url : "Fix the affected elements and re-test with a keyboard and a screen reader.",
          source: `${evidence.engine || "axe-core"} · ${violation.id}`,
        });
      }
    }
  }
  return items.sort((a, b) => (order[String(a.severity).toLowerCase()] ?? 5) - (order[String(b.severity).toLowerCase()] ?? 5));
}
