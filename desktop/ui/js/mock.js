// Browser-only mock backend: representative data for UI development and
// design review outside the Tauri shell. Never used inside the app.

const settings = {
  relayCommandOverride: "",
  loginMethod: "browser",
  startRelayOnLaunch: true,
  autoRestartRelay: true,
  host: "0.0.0.0",
  port: 8317,
  requireBearerAuth: true,
  autoGenerateBearerToken: false,
  browserOpen: true,
  loginTimeoutSeconds: 900,
  upstreamBaseUrl: "https://chatgpt.com/backend-api/codex",
  issuerBaseUrl: "https://auth.openai.com",
  clientId: "app_EMoamEEZ73f0CkXaXp7hrann",
  clientVersion: "0.124.0",
  requestTimeoutSeconds: 120,
  rateLimitPerMinute: 120,
  rateLimitBurst: 40,
  concurrentRequestsPerIp: 50,
  failedAuthWindowSeconds: 300,
  failedAuthMaxAttempts: 8,
  failedAuthBlockSeconds: 900,
  trustXForwardedFor: false,
  networkScope: "private",
  maxUploadBytes: 33554432,
  maxTotalUploadBytes: 268435456,
  enableOpenaiProvider: true,
  modelsCacheTtlSeconds: 300,
  openaiBalance: "balanced",
  openaiExtraModelsCsv: "gpt-5.6-sol, gpt-5.6-terra",
  openaiRetryAttempts: 3,
  openaiRetryBackoffCsv: "5, 20, 60",
  enableClaude: true,
  claudeBin: "claude",
  claudeTimeoutSeconds: 600,
  claudeMaxConcurrentRequests: 2,
  claudeBalance: "balanced",
  claudeAccountCooldownSeconds: 300,
  claudeStripApiKeyEnv: true,
  claudeModelsCsv: "claude:sonnet, claude:opus, claude:haiku, claude:fable",
  extraServeArgs: "",
};

let managed = true;
let mockAutostart = false;
let mockResetRedeemed = false;
let mockToken = "airtk_mock_2f9c8e1a7b3d4c5e6f708192a3b4c5d6";
let claudeAccounts = [
  {slug: "claude-perso", email: "perso@claude.ai", subscription_type: "pro", ready_for_requests: true, managed_profile: true},
  {slug: "claude-work", email: "work@claude.ai", subscription_type: "max", ready_for_requests: true, managed_profile: true},
];

const state = () => ({
  lifecycle: managed ? "running" : "stopped",
  reachable: managed,
  managed,
  app_version: "0.0.0-mock",
  auth_mismatch: false,
  login_url: null,
  login_code: null,
  login_provider: null,
  login_accepts_code: false,
  login_running: false,
  claude_effective: settings.enableClaude && !settings.trustXForwardedFor,
  claude_token_present: false,
  settings: { ...settings },
  relay_status: managed
    ? {
        version: "0.2.5",
        ready: { relay_token: true, any_provider: true },
        providers: {
          openai: {
            enabled: true,
            ready_for_requests: true,
            email: "perso@gmail.com",
            plan_type: "plus",
            balance: "balanced",
            // The two real plan shapes observed live (2026-07): a Plus
            // account (weekly window only) and an Enterprise account (5h +
            // weekly). The tally is anchored to each account's longest
            // window, hence the weekly window_label on both breakdowns.
            accounts: [
              { slug: "default", email: "perso@gmail.com", plan_type: "plus", ready_for_requests: true, limited: false,
                window_tokens: {
                  object: "relay_window_tokens",
                  scope: "current_usage_window_via_this_relay",
                  window_reset_at: 1785748463,
                  window_seconds: 604800,
                  window_label: "weekly",
                  models: [
                    { model: "gpt-5.5", input_tokens: 1234567, output_tokens: 89012, cached_input_tokens: 800000 },
                    { model: "gpt-5.4-mini", input_tokens: 456789, output_tokens: 12345, cached_input_tokens: 300000 },
                  ],
                  totals: { input_tokens: 1691356, output_tokens: 101357, cached_input_tokens: 1100000 },
                } },
              { slug: "work-x", email: "work@company.com", plan_type: "enterprise", ready_for_requests: true, limited: false,
                window_tokens: {
                  object: "relay_window_tokens",
                  scope: "current_usage_window_via_this_relay",
                  window_reset_at: 1785714800,
                  window_seconds: 604800,
                  window_label: "weekly",
                  models: [
                    { model: "gpt-5.5", input_tokens: 84210, output_tokens: 6021, cached_input_tokens: 41000 },
                  ],
                  totals: { input_tokens: 84210, output_tokens: 6021, cached_input_tokens: 41000 },
                },
                // Image-generation quota (its own 24h upstream bucket) as last
                // read from an image response, plus the relay's own count.
                image_usage: {
                  object: "relay_image_usage",
                  limit_id: "imagegen_premium",
                  used_percent: 12,
                  window_seconds: 86400,
                  window_label: "24h",
                  reset_at: Math.floor(Date.now() / 1000) + 80000,
                  observed_at: new Date().toISOString(),
                  source: "image_response",
                  relay: { scope: "last_window_via_this_relay", window_seconds: 86400, requests: 4, images: 5, image_tokens: 2890 },
                } },
              // Exhausted Plus-shaped account: keeps the "At limit" badge,
              // the full-bar tint, and the empty "more" panel reviewable in
              // mock mode (no window_tokens on purpose).
              { slug: "backup", email: "backup@gmail.com", plan_type: "plus", ready_for_requests: true,
                limited: true, limited_for_seconds: 172800 },
              // Upstream-invalidated sign-in: the relay's readiness flags
              // still say ready (they only check credentials exist on
              // disk), but the usage probe fails — mirrors the live bug
              // where "Ready" sat next to "Usage unavailable".
              { slug: "expired", email: "expired@gmail.com", plan_type: "plus",
                ready_for_requests: true, limited: false },
            ],
          },
          claude: {
            enabled: true,
            ready_for_requests: true,
            email: "perso@claude.ai",
            subscription_type: "pro",
            cli_version: "2.1.0",
            balance: settings.claudeBalance,
            accounts: claudeAccounts.map((account) => ({...account})),
          },
        },
      }
    : null,
  local_endpoint: `http://127.0.0.1:${settings.port}/v1`,
  lan_endpoints: settings.host === "0.0.0.0" ? [`http://192.168.1.146:${settings.port}/v1`] : [],
  // Documentation-range address (RFC 5737), only while the scope is "any",
  // like the core's public-IP lookup.
  public_endpoint: settings.networkScope === "any" && settings.host !== "127.0.0.1"
    ? `http://203.0.113.10:${settings.port}/v1`
    : null,
  public_ip_pending: false,
  config_path: "~/.config/airelays/config.toml",
  logs_dir: "~/.airelays/logs",
});

const consoleEntries = [
  { at_ms: Date.now() - 60000, source: "app", text: "AIRelays desktop ready.", is_error: false },
  { at_ms: Date.now() - 42000, source: "relay", text: "Starting: python3 -m airelays serve --config ~/.config/airelays/config.toml", is_error: false },
  { at_ms: Date.now() - 41000, source: "relay", text: "INFO: Uvicorn running on http://0.0.0.0:8317 (Press CTRL+C to quit)", is_error: false },
  { at_ms: Date.now() - 12000, source: "doctor", text: "relay token: OK", is_error: false },
  { at_ms: Date.now() - 11000, source: "doctor", text: "upstream /models probe: OK (14 models)", is_error: false },
  { at_ms: Date.now() - 8000, source: "openai-login", text: "OSError: Unable to bind the AIRelays login callback server on localhost:1455.", is_error: true },
];

const traffic = [
  {
    id: "req-1", last_seen: new Date(Date.now() - 30000).toISOString(),
    method: "POST", path: "/v1/chat/completions", provider: "openai",
    model: "gpt-5.5", status_code: 200, last_phase: "outbound_response",
    event_count: 4, input_tokens: 42, output_tokens: 128, account: "work@company.com",
    details: '{\n  "request_id": "req-1",\n  "phase": "outbound_response",\n  "status_code": 200,\n  "model": "gpt-5.5"\n}',
  },
  {
    id: "req-2", last_seen: new Date(Date.now() - 90000).toISOString(),
    method: "GET", path: "/v1/models", provider: "openai",
    model: "-", status_code: 200, last_phase: "outbound_response",
    event_count: 2, input_tokens: null, output_tokens: null, account: "perso@gmail.com",
    details: '{\n  "phase": "outbound_response",\n  "status_code": 200\n}',
  },
  {
    id: "req-3", last_seen: new Date(Date.now() - 200000).toISOString(),
    method: "POST", path: "/v1/chat/completions", provider: "openai",
    model: "gpt-5.5", status_code: 401, last_phase: "inbound_rejected",
    event_count: 1, input_tokens: null, output_tokens: null, account: "perso@gmail.com",
    details: '{\n  "phase": "inbound_rejected",\n  "status_code": 401,\n  "reason": "invalid bearer token"\n}',
  },
];

// ---- Traffic polling (traffic view): each get_traffic call adds one new,
// deterministic newest request (req-live-1, req-live-2, ...) and keeps a
// bounded window, like the relay's retained log tail, so selection and
// scroll behavior can be exercised across polls.
const TRAFFIC_WINDOW = 12;
let trafficPolls = 0;

function mockTraffic() {
  trafficPolls += 1;
  const n = trafficPolls;
  traffic.unshift({
    id: `req-live-${n}`, last_seen: new Date(Date.now()).toISOString(),
    method: "POST", path: "/v1/responses", provider: "openai",
    model: "gpt-5.5", status_code: 200, last_phase: "outbound_response",
    event_count: 2, input_tokens: 10 + n, output_tokens: 20 + n, account: "work@company.com",
    details: `{\n  "request_id": "req-live-${n}",\n  "phase": "inbound_request",\n  "path": "/v1/responses"\n}\n\n{\n  "request_id": "req-live-${n}",\n  "phase": "outbound_response",\n  "status_code": 200\n}`,
  });
  traffic.length = Math.min(traffic.length, TRAFFIC_WINDOW);
  return structuredClone(traffic);
}
// ---- end traffic polling

let logRetention = {
  policy: { retention_days: 7, max_total_mb: 1024, max_file_mb: 50 },
  usage_bytes: 73400320, file_count: 24, over_budget: false, last_error: null,
};

export async function mockInvoke(command, args = {}) {
  switch (command) {
    case "get_state":
      return state();
    case "get_log_retention":
      return structuredClone(logRetention);
    case "set_log_retention":
      logRetention.policy = structuredClone(args.policy);
      return structuredClone(logRetention);
    case "get_balance":
      return { object: "relay.balance", openai: settings.openaiBalance, claude: settings.claudeBalance,
        strategies: ["balanced", "weighted", "round_robin", "ping_pong", "ordered"], runtime_only: true };
    case "set_balance":
      if (args.changes.openai) settings.openaiBalance = args.changes.openai;
      if (args.changes.claude) settings.claudeBalance = args.changes.claude;
      return mockInvoke("get_balance");
    case "save_settings":
      Object.assign(settings, args.settings);
      return null;
    case "start_relay":
      managed = true;
      return 12345;
    case "stop_relay":
      managed = false;
      return null;
    case "restart_relay":
      managed = true;
      return 12345;
    case "set_auth_mode":
      // Same rule as AppSettings::validate: internet access needs the key.
      if (!args.requireToken && settings.networkScope === "any" && settings.host !== "127.0.0.1") {
        throw "\"Anyone on the internet\" requires the API key: keep Protected mode, or limit who can connect first.";
      }
      settings.requireBearerAuth = args.requireToken;
      return null;
    case "set_network_scope":
      settings.host = args.scope === "loopback" ? "127.0.0.1" : "0.0.0.0";
      settings.networkScope = args.scope;
      // Internet access forces the API key on in the same change.
      if (args.scope === "any") {
        settings.requireBearerAuth = true;
      }
      return null;
    case "set_network_exposure":
      // Pre-scope command, kept as a shim like the Rust one.
      settings.host = args.exposed ? "0.0.0.0" : "127.0.0.1";
      settings.networkScope = args.exposed ? "private" : "loopback";
      return null;
    case "get_console":
      return consoleEntries;
    case "clear_console":
      consoleEntries.length = 0;
      return null;
    case "get_traffic":
      return mockTraffic(); // grows by one newest request per poll
    case "token_action":
      // Remembers rotations and custom keys, like the real key file.
      if (args.action === "rotate") {
        mockToken = "airtk_rotated_9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a";
      }
      return mockToken;
    case "run_doctor":
      return true;
    case "get_usage":
      // Mirrors the relay's normalized all_accounts payload shape.
      return {
        object: "subscription_status_list",
        claude: {
          object: "subscription_status_list",
          provider: "claude",
          accounts: claudeAccounts.map((account, index) => ({
            slug: account.slug,
            email: account.email,
            status: {
              object: "subscription_status", provider: "claude",
              captured_at: new Date().toISOString(),
              account: {email: account.email, plan_type: account.subscription_type},
              rate_limits: {
                default: {
                  primary_window: {used_percent: 22 + index * 10, window_seconds: 18000,
                    window_label: "5h", reset_after_seconds: 9000,
                    reset_at_iso: new Date(Date.now() + 9000 * 1000).toISOString()},
                  secondary_window: {used_percent: 61 - index * 20, window_seconds: 604800,
                    window_label: "weekly", reset_after_seconds: 400000,
                    reset_at_iso: new Date(Date.now() + 400000 * 1000).toISOString()},
                },
                additional: [],
              },
            },
          })),
          account: { email: "perso@claude.ai", plan_type: "pro" },
          rate_limit_reached_type: null,
          rate_limits: {
            default: {
              allowed: null,
              limit_reached: false,
              primary_window: {
                used_percent: 22,
                window_seconds: 18000,
                window_label: "5h",
                reset_after_seconds: 9000,
              },
              secondary_window: {
                used_percent: 61,
                window_seconds: 604800,
                window_label: "weekly",
                reset_after_seconds: 400000,
              },
            },
            additional: [],
          },
        },
        accounts: [
          {
            slug: "default",
            email: "perso@gmail.com",
            status: {
              account: { email: "perso@gmail.com", plan_type: "plus" },
              rate_limit_reached_type: null,
              rate_limits: {
                default: {
                  allowed: true,
                  limit_reached: false,
                  // Current Plus shape (observed live 2026-07): the weekly
                  // window arrives ALONE in the primary slot — the plan has
                  // no 5h window at all, so the card renders exactly one
                  // bar. High usage exercises the "high" bar tint.
                  primary_window: {
                    used_percent: 91,
                    window_seconds: 604800,
                    window_label: "weekly",
                    reset_after_seconds: 551663,
                  },
                },
                additional: [],
              },
            },
          },
          {
            slug: "work-x",
            email: "work@company.com",
            status: {
              account: { email: "work@company.com", plan_type: "enterprise" },
              captured_at: new Date().toISOString(),
              rate_limit_reached_type: null,
              rate_limits: {
                default: {
                  allowed: true,
                  limit_reached: false,
                  // Current Enterprise shape: the classic 5h primary window
                  // plus the weekly secondary, both nearly untouched.
                  // reset_at values exercise the absolute stamps in the
                  // bar details and the "more" facts.
                  primary_window: {
                    used_percent: 0,
                    window_seconds: 18000,
                    window_label: "5h",
                    reset_after_seconds: 18000,
                    reset_at: Math.floor(Date.now() / 1000) + 18000,
                    reset_at_iso: new Date(Date.now() + 18000 * 1000).toISOString(),
                  },
                  secondary_window: {
                    used_percent: 4,
                    window_seconds: 604800,
                    window_label: "weekly",
                    reset_after_seconds: 518000,
                    reset_at: Math.floor(Date.now() / 1000) + 518000,
                    reset_at_iso: new Date(Date.now() + 518000 * 1000).toISOString(),
                  },
                },
                // Separate top-level quota in the upstream payload; renders
                // as its own labeled bar.
                code_review: {
                  allowed: true,
                  limit_reached: false,
                  primary_window: {
                    used_percent: 12,
                    window_seconds: 604800,
                    window_label: "weekly",
                    reset_after_seconds: 518000,
                  },
                },
                // The image-generation quota: a separate 24h upstream bucket
                // the relay reads from image responses (or a free probe).
                image_generation: {
                  limit_id: "imagegen_premium",
                  allowed: true,
                  limit_reached: false,
                  source: "image_response",
                  observed_at: new Date().toISOString(),
                  primary_window: {
                    used_percent: 12,
                    window_seconds: 86400,
                    window_minutes: 1440,
                    window_label: "24h",
                    reset_after_seconds: 80000,
                    reset_at: Math.floor(Date.now() / 1000) + 80000,
                    reset_at_iso: new Date(Date.now() + 80000 * 1000).toISOString(),
                  },
                  secondary_window: null,
                  relay: { scope: "last_window_via_this_relay", window_seconds: 86400, requests: 4, images: 5, image_tokens: 2890 },
                },
                additional: [],
              },
              credits: {
                has_credits: true,
                unlimited: false,
                overage_limit_reached: false,
                balance: "250",
              },
              spend_control: { reached: false, individual_limit: null },
              rate_limit_reset_credits: { available_count: 2, applicable_available_count: 2 },
            },
          },
          {
            slug: "backup",
            email: "backup@gmail.com",
            status: {
              account: { email: "backup@gmail.com", plan_type: "plus" },
              rate_limit_reached_type: "primary",
              rate_limits: {
                default: {
                  allowed: false,
                  limit_reached: true,
                  // Plus shape at its weekly limit: exercises the "full"
                  // bar tint alongside the At-limit badge.
                  primary_window: {
                    used_percent: 100,
                    window_seconds: 604800,
                    window_label: "weekly",
                    reset_after_seconds: 172800,
                  },
                },
                additional: [],
              },
            },
          },
          // The live failure shape from an upstream-invalidated sign-in:
          // no status, just the relay's raw error string.
          {
            slug: "expired",
            email: "expired@gmail.com",
            error:
              'Token refresh failed: 401 {\n  "error": {\n    "message": "Your session has ended. Please log in again.",\n    "type": "invalid_request_error",\n    "param": null,\n    "code": "refresh_token_invalidated"\n  }\n}',
          },
        ],
      };
    case "set_login_method":
      settings.loginMethod = args.method;
      return null;
    case "logout_account":
      return null;
    case "refresh_accounts":
      return { accounts: [] };
    case "get_reset_credits":
      // Mirrors /v1/relay/accounts/resets: one held reset that the upstream
      // would apply now, one account with nothing, one unreadable.
      return {
        object: "accounts.resets",
        available_count: mockResetRedeemed ? 0 : 1,
        accounts: [
          {
            slug: "default",
            email: "perso@gmail.com",
            resets: {
              object: "rate_limit_reset_credits",
              available_count: mockResetRedeemed ? 0 : 1,
              applicable_available_count: mockResetRedeemed ? 0 : 1,
              redeemable_now: !mockResetRedeemed,
              total_earned_count: 0,
              next_expiry_at: mockResetRedeemed ? null : new Date(Date.now() + 20 * 86400 * 1000).toISOString(),
              credits: mockResetRedeemed ? [] : [{
                id: "RateLimitResetCredit_mock1",
                reset_type: "codex_rate_limits",
                status: "available",
                supported_by_plan: true,
                title: "Full reset",
                description: "Thanks for using Codex! You've been granted one free rate limit reset.",
                granted_by: "Codex Team",
                granted_at: new Date(Date.now() - 10 * 86400 * 1000).toISOString(),
                expires_at: new Date(Date.now() + 20 * 86400 * 1000).toISOString(),
                expires_in_seconds: 20 * 86400,
                expired: false,
              }],
            },
          },
          {
            slug: "backup",
            email: "backup@gmail.com",
            resets: {
              object: "rate_limit_reset_credits",
              available_count: 0, applicable_available_count: 0, redeemable_now: false,
              total_earned_count: 0, next_expiry_at: null, credits: [],
            },
          },
          { slug: "expired", email: "expired@gmail.com", error: "Token refresh failed: 401" },
        ],
      };
    case "redeem_reset_credit":
      mockResetRedeemed = true;
      return {
        object: "accounts.reset_redeem", slug: "default", email: args.account,
        outcome: "reset", known_outcome: true, reset_applied: true, windows_reset: 1,
        credit_id: args.creditId ?? "RateLimitResetCredit_mock1", status: null,
      };
    case "get_autostart":
      return mockAutostart;
    case "set_autostart":
      mockAutostart = args.enabled;
      return null;
    case "clear_claude_token":
      return true;
    case "logout_claude":
      claudeAccounts = claudeAccounts.filter((account) => account.slug !== args.account);
      return { token_removed: args.account === "default", cli_signed_out: true, cli_error: null };
    case "cancel_login":
      return null;
    case "get_models": {
      // Mirrors the relay's `/v1/models` metadata: account availability,
      // reasoning, structured output and the API list-price reference.
      const reasoning = (modes, fallback) => ({ parameter: "reasoning_effort", modes, default: fallback });
      const structured = (types) => ({ parameter: "response_format", types });
      const price = (family, input, cached, output, tier, source) => ({
        input_per_million: input, cached_input_per_million: cached, output_per_million: output,
        currency: "USD", unit: "per_million_tokens", as_of: "2026-10-25", matched_family: family, cost_tier: tier,
        basis: "Standard API list price.",
        source: source === "claude" ? "https://platform.claude.com/docs/en/about-claude/pricing" : "https://developers.openai.com/api/docs/pricing",
      });
      const gpt = ["low", "medium", "high", "xhigh", "max"];
      const claude = ["low", "medium", "high", "xhigh", "max"];
      const both = ["json_schema", "json_object"];
      const openai = (id, extra) => ({ id, object: "model", owned_by: "airelays-openai-subscription", airelays: {
        provider: "openai", discovery_source: "upstream_catalog", account_availability: { supported: 2, total: 2 },
        catalog_visibility: "list", reasoning: reasoning(gpt, "medium"), structured_output: structured(both), pricing: null, ...extra } });
      const anthropic = (id, extra) => ({ id, object: "model", owned_by: "airelays-claude-subscription", airelays: {
        provider: "claude", discovery_source: "claude_cli", account_availability: null, resolved_model: id,
        reasoning: reasoning(claude, null), structured_output: structured(both), pricing: null, ...extra } });
      return {
        object: "list",
        data: [
          openai("gpt-6-astra", { display_name: "GPT-6-Astra", reasoning: reasoning([...gpt, "ultra"], "medium"), pricing: price("gpt-6-astra", 10, 1, 50, 5) }),
          openai("gpt-reserve", { display_name: "GPT-Reserve", catalog_visibility: "hide" }),
          openai("gpt-5.6-sol", { display_name: "GPT-5.6-Sol", reasoning: reasoning([...gpt, "ultra"], "low"), account_availability: { supported: 1, total: 2 }, pricing: price("gpt-5.6-sol", 4, 0.4, 20, 4) }),
          openai("gpt-5.6-terra", { display_name: "GPT-5.6-Terra", pricing: price("gpt-5.6-terra", 2, 0.2, 12, 3) }),
          openai("gpt-5.6-luna", { display_name: "GPT-5.6-Luna", pricing: price("gpt-5.6-luna", 0.2, 0.02, 1.2, 2) }),
          openai("codex-auto-review", { display_name: "Codex Auto Review", catalog_visibility: "hide" }),
          openai("gpt-5.5", { discovery_source: "configured", account_availability: null, catalog_visibility: null, reasoning: reasoning(["none", "low", "medium", "high", "xhigh"], "none"), pricing: price("gpt-5.5", 5, 0.5, 30, 4) }),
          openai("gpt-image-2", { display_name: "GPT Image 2", discovery_source: "configured", account_availability: null, catalog_visibility: null, reasoning: reasoning([], null), structured_output: structured([]), pricing: price("gpt-image-2", 5, 1.25, 30, 4) }),
          anthropic("claude-sonnet-5-5", { display_name: "Sonnet 5.5", pricing: price("claude-sonnet-5-5", 2, 0.1, 10, 3, "claude") }),
          anthropic("claude:sonnet", { display_name: "Sonnet 5.5", resolved_model: "claude-sonnet-5-5", pricing: price("claude-sonnet-5-5", 2, 0.1, 10, 3, "claude") }),
          anthropic("claude:opus", { display_name: "Opus 5.5", resolved_model: "claude-opus-5-5", pricing: price("claude-opus-5-5", 4, 0.2, 20, 4, "claude") }),
          anthropic("claude:haiku", { display_name: "Haiku 5.5", resolved_model: "claude-haiku-5-5", pricing: price("claude-haiku-5-5", 0.1, 0.01, 0.5, 1, "claude") }),
          anthropic("claude-opus-5-5", { display_name: "Opus 5.5", pricing: price("claude-opus-5-5", 4, 0.2, 20, 4, "claude") }),
          anthropic("claude-haiku-4-5-20251001", { display_name: "Haiku 4.5", reasoning: reasoning([], null), pricing: price("claude-haiku-4-5", 1, 0.1, 5, 2, "claude") }),
        ],
      };
    }
    case "set_custom_token":
      mockToken = args.token;
      return null;
    case "set_claude_token":
    case "submit_login_code":
    case "open_path":
    case "open_url":
    case "install_update":
      return null;
    case "take_update_result": {
      // ?mockUpdate=ok|failed previews the dashboard after an update.
      const mode = new URLSearchParams(location.search).get("mockUpdate");
      return {
        after_update: Boolean(mode),
        failure: mode === "failed" ? "Checksum mismatch for AIRelays_0.19.0_x64-setup.exe." : null,
      };
    }
    case "check_for_update":
      return {
        current: "0.0.0-mock",
        latest: "0.19.0",
        update_available: true,
        release_url: "https://github.com/lpalbou/AIRelays/releases/tag/v0.19.0",
        release_notes: "### Added\n\n- Desktop: an About page with project links and one-click updates.",
        published_at: "2026-10-18T09:00:00Z",
        can_install: true,
        install_hint: null,
        log_path: "~/.airelays/update/update.log",
      };
    case "run_login":
      if (args.provider === "claude" && !args.account) {
        const number = claudeAccounts.length + 1;
        claudeAccounts.push({slug: `claude-${number}`, email: `account${number}@claude.ai`,
          subscription_type: "pro", ready_for_requests: true, managed_profile: true});
      }
      return null;
    default:
      throw new Error(`Unknown mock command: ${command}`);
  }
}
