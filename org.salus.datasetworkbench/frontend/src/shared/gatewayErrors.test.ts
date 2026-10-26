import assert from "node:assert/strict";
import { test } from "node:test";
import { explainFailure } from "./gatewayErrors.ts";

test("a message from the workbench is shown as it is, whatever the status", () => {
  assert.equal(explainFailure(404, "No such folder.", "Not Found", "POST", "/groups"), "No such folder.");
  assert.equal(explainFailure(409, "'a' already exists here.", "Conflict", "POST", "/groups"), "'a' already exists here.");
});

test("an empty 404 says that Salus lost the connection and names the request", () => {
  const text = explainFailure(404, "", "Not Found", "POST", "/groups?x=1");
  assert.match(text, /404 for POST \/groups\)/);
  assert.match(text, /Reload the Salus page/);
  assert.doesNotMatch(text, /x=1/); // no query strings in messages
});

test("other empty answers get their own advice", () => {
  assert.match(explainFailure(401, "  ", "Unauthorized", "GET", "/tree"), /login/);
  assert.match(explainFailure(504, "", "Gateway Timeout", "GET", "/tree"), /did not answer/);
  assert.equal(explainFailure(418, "", "I'm a teapot", "GET", "/x"), "418 I'm a teapot");
});
