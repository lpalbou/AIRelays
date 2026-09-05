import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

function element() {
  return {
    children: [], style: {}, textContent: "", classList: { add() {} },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); },
    setAttribute() {}, addEventListener() {},
  };
}

function view() {
  const context = vm.createContext({
    document: { createElement: element, createTextNode: (text) => ({ textContent: text }) },
    icon: () => "", Date,
  });
  const source = readFileSync(new URL("../ui/js/views/overview.js", import.meta.url), "utf8")
    .replace(/^import .*;$/gm, "").replace("export const overviewView", "const overviewView");
  vm.runInContext(source, context);
  return context;
}

test("money uses explicit minor-unit scale, never invents a balance", () => {
  const ctx = view();
  assert.equal(ctx.formatMinorMoney({ amount_minor: 125, currency: "USD", exponent: 2 }), "USD 1.25");
  assert.equal(ctx.formatMinorMoney({ amount_minor: 125, currency: "USD" }), null);
  const facts = ctx.usageFacts({ spend: {
    enabled: false, disabled_reason: "out_of_credits", balance: null,
    used: { amount_minor: 0, currency: "USD", exponent: 2 },
  }, rate_limit_reset_credits: { available_count: 3, applicable_available_count: 0 } });
  assert.ok(facts.some(([label, value]) => label === "usage credits" && value === "disabled (out of credits)"));
  assert.ok(facts.some(([label, value]) => label === "credit spend" && value === "USD 0.00"));
  assert.ok(!facts.some(([label]) => label === "credit balance"));
  assert.ok(facts.some(([label, value]) => label === "limit-reset credits" && value === "3 available; 0 usable now"));
});

test("expired and unknown windows never show a healthy zero", () => {
  const ctx = view();
  for (const window of [{used_percent: null}, {used_percent: 100, reset_at: 1}]) {
    const row = ctx.usageWindowRow("Weekly", window);
    assert.equal(row.children[0].children[1].textContent, "awaiting fresh data");
    assert.equal(row.children[1].children.length, 0);
  }
});

test("Fable limit does not label all Claude models exhausted", () => {
  const ctx = view();
  vm.runInContext(`claudeUsage = {
    rate_limit_reached_type: null,
    rate_limits: {default: {primary_window: {used_percent: 8, window_label: "5h"}}, additional: [{
      limit_name: "Fable", rate_limit: {limit_reached: true,
        primary_window: {used_percent: 100, window_label: "weekly"}}
    }]}
  }`, ctx);
  const block = ctx.claudeBlock({ready_for_requests: true}, false);
  assert.equal(block.children[0].children[2].textContent, "Fable at limit");
  assert.equal(block.children[2].children[0].children[0].textContent, "Fable · Weekly");
});
