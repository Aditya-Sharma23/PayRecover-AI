import assert from "node:assert/strict";
import { test } from "node:test";
import { collectFindings, groupChecks, groupStatus } from "../src/lib/report.js";

const report = {
  checks: [
    { check_id: "http_status", status: "passed", evidence: {} },
    { check_id: "cookie_flags", status: "warning", evidence: { findings: [{ title: "Cookie sid", severity: "low", detail: "d", recommendation: "r" }] } },
    { check_id: "exposed_resources", status: "failed", evidence: { findings: [{ title: "Exposed .env", severity: "medium", detail: "d", recommendation: "r" }] } },
    { check_id: "accessibility", status: "failed", evidence: { engine: "axe-core 4.10.3", violations: [{ id: "image-alt", impact: "critical", help: "Images need alt", nodes: 1, targets: ["img"] }] } },
  ],
};

test("worst status wins and empty groups are not run", () => {
  assert.equal(groupStatus([]), "not_run");
  assert.equal(groupStatus([{ status: "passed" }, { status: "warning" }, { status: "failed" }]), "failed");
});

test("checks land in their dashboard category", () => {
  const groups = Object.fromEntries(groupChecks(report).map((group) => [group.key, group]));
  assert.equal(groups.response.status, "passed");
  assert.equal(groups.security.checks.length, 2);
  assert.equal(groups.security.status, "failed");
  assert.equal(groups.contract.status, "not_run");
});

test("findings combine security leads and axe violations, most severe first", () => {
  const items = collectFindings(report);
  assert.deepEqual(items.map((item) => item.title), ["Images need alt", "Exposed .env", "Cookie sid"]);
});
