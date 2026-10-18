import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

function element() {
  return {
    children: [], style: {}, dataset: {}, textContent: "", classList: { add() {} },
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
  const usage = {
    rate_limit_reached_type: null,
    rate_limits: {default: {primary_window: {used_percent: 8, window_label: "5h"}}, additional: [{
      limit_name: "Fable", rate_limit: {limit_reached: true,
        primary_window: {used_percent: 100, window_label: "weekly"}}
    }]}
  };
  const block = ctx.claudeBlock({ready_for_requests: true}, false, usage);
  assert.equal(block.children[0].children[2].textContent, "Fable at limit");
  assert.equal(block.children[2].children[0].children[0].textContent, "Fable · Weekly");
});

test("the image quota renders as its own Images meter", () => {
  const ctx = view();
  const windows = ctx.usageWindows({rate_limits: {
    default: {primary_window: {used_percent: 8, window_label: "5h"}},
    image_generation: {limit_id: "imagegen_premium", source: "probe",
      primary_window: {used_percent: 12, window_label: "24h", reset_after_seconds: 80000}, secondary_window: null},
  }});
  // Arrays come from the vm realm: compare by value, not prototype.
  assert.equal(JSON.stringify(windows.map(([label]) => label)), JSON.stringify(["5h window", "Images · 24h window"]));
  const row = ctx.usageWindowRow(...windows[1]);
  assert.equal(row.children[0].children[1].children[0].textContent, "12% used");
  const facts = ctx.usageFacts({rate_limits: {image_generation: {source: "probe", observed_at: new Date().toISOString()}}});
  assert.ok(facts.some(([label, value]) => label === "image quota reading" && value.startsWith("probe · ")));
});

test("the account details name the images made through this relay", () => {
  const ctx = view();
  const usage = {window_label: "24h", relay: {images: 5, image_tokens: 2890, window_seconds: 86400}};
  const withTokens = ctx.windowTokensDetail({window_label: "weekly", models: [
    {model: "gpt-5.5", input_tokens: 10, output_tokens: 2}], totals: {input_tokens: 10, output_tokens: 2}},
    null, true, "a", "a", usage);
  const panel = withTokens.children[1];
  assert.ok(panel.children.some((child) => child.textContent === "Images: 5 generated via this relay in the last 24h (2.9k image tokens)."));
  const noTokens = ctx.windowTokensDetail(null, null, true, "b", "b", usage);
  assert.ok(noTokens.children[1].children.some((child) => String(child.textContent).startsWith("Images: 5 generated")));
  const plain = ctx.windowTokensDetail(null, null, true, "c", "c", null);
  assert.ok(!plain.children[1].children.some((child) => String(child.textContent).startsWith("Images:")));
});
