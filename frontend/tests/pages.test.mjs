import assert from "node:assert/strict";
import test from "node:test";
import { PAGES, pageFor, sectionFor } from "../src/lib/pages.js";

test("each sidebar entry is a page chosen by the address", () => {
  assert.equal(pageFor("#disputes"), "disputes");
  assert.equal(pageFor(""), "dashboard");
  assert.equal(pageFor("#nothing-here"), "dashboard");
  assert.ok(Object.keys(PAGES).includes("recoveries"));
});

test("links to a section open the page that holds it", () => {
  assert.equal(pageFor("#settings"), "more");
  assert.equal(pageFor("#report"), "more");
  assert.equal(pageFor("#scan"), "check");
});

test("Ask PayRecover and Alerts live on the dashboard, and their old links scroll to them", () => {
  assert.ok(!("assistant" in PAGES) && !("alerts" in PAGES));
  assert.equal(pageFor("#assistant"), "dashboard");
  assert.equal(pageFor("#alerts"), "dashboard");
  assert.equal(sectionFor("#alerts"), "alerts");
  assert.equal(sectionFor("#disputes"), null);
});
