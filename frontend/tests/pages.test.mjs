import assert from "node:assert/strict";
import test from "node:test";
import { PAGES, pageFor } from "../src/lib/pages.js";

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
