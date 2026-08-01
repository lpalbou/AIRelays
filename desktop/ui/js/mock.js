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
  authStorageMode: "auto",
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
  claudeStripApiKeyEnv: true,
  claudeModelsCsv: "claude:sonnet, claude:opus, claude:haiku, claude:fable",
  extraServeArgs: "",
};

let managed = true;
let mockAutostart = false;

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
  claude_effective:
    settings.enableClaude &&
    ["127.0.0.1", "localhost", "::1"].includes(settings.host) &&
    !settings.trustXForwardedFor,
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
                } },
              // Exhausted Plus-shaped account: keeps the "At limit" badge,
              // the full-bar tint, and the empty "more" panel reviewable in
              // mock mode (no window_tokens on purpose).
              { slug: "backup", email: "backup@gmail.com", plan_type: "plus", ready_for_requests: true,
                limited: true, limited_for_seconds: 172800 },
            ],
          },
          claude: {
            enabled: true,
            ready_for_requests: true,
            email: "perso@claude.ai",
            subscription_type: "pro",
            cli_version: "2.1.0",
          },
        },
      }
    : null,
  local_endpoint: `http://127.0.0.1:${settings.port}/v1`,
  lan_endpoints: settings.host === "0.0.0.0" ? [`http://192.168.1.146:${settings.port}/v1`] : [],
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
    details: '{\n  "phase": "outbound_response",\n  "status_code": 200,\n  "model": "gpt-5.5"\n}',
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

export async function mockInvoke(command, args = {}) {
  switch (command) {
    case "get_state":
      return state();
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
      settings.requireBearerAuth = args.requireToken;
      return null;
    case "set_network_exposure":
      settings.host = args.exposed ? "0.0.0.0" : "127.0.0.1";
      return null;
    case "get_console":
      return consoleEntries;
    case "clear_console":
      consoleEntries.length = 0;
      return null;
    case "get_traffic":
      return traffic;
    case "token_action":
      return args.action === "rotate"
        ? "airtk_rotated_9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a"
        : "airtk_mock_2f9c8e1a7b3d4c5e6f708192a3b4c5d6";
    case "run_doctor":
      return true;
    case "get_usage":
      // Mirrors the relay's normalized all_accounts payload shape.
      return {
        object: "subscription_status_list",
        claude: {
          object: "subscription_status",
          provider: "claude",
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
              rate_limit_reached_type: null,
              rate_limits: {
                default: {
                  allowed: true,
                  limit_reached: false,
                  // Current Enterprise shape: the classic 5h primary window
                  // plus the weekly secondary, both nearly untouched.
                  primary_window: {
                    used_percent: 0,
                    window_seconds: 18000,
                    window_label: "5h",
                    reset_after_seconds: 18000,
                  },
                  secondary_window: {
                    used_percent: 4,
                    window_seconds: 604800,
                    window_label: "weekly",
                    reset_after_seconds: 518000,
                  },
                },
                additional: [],
              },
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
        ],
      };
    case "set_login_method":
      settings.loginMethod = args.method;
      return null;
    case "logout_account":
      return null;
    case "refresh_accounts":
      return { accounts: [] };
    case "get_autostart":
      return mockAutostart;
    case "set_autostart":
      mockAutostart = args.enabled;
      return null;
    case "clear_claude_token":
      return true;
    case "logout_claude":
      return { token_removed: true, cli_signed_out: true, cli_error: null };
    case "cancel_login":
      return null;
    case "get_models":
      return {
        object: "list",
        data: [
          { id: "gpt-5.5", object: "model", airelays: { provider: "openai", reasoning: { parameter: "reasoning_effort", modes: ["none", "low", "medium", "high", "xhigh"], default: "none" }, structured_output: { parameter: "response_format", types: ["json_schema"] } } },
          { id: "gpt-5.4", object: "model", airelays: { provider: "openai", reasoning: { parameter: "reasoning_effort", modes: ["none", "low", "medium", "high", "xhigh"], default: "none" }, structured_output: { parameter: "response_format", types: ["json_schema"] } } },
          { id: "gpt-5.4-mini", object: "model", airelays: { provider: "openai", reasoning: { parameter: "reasoning_effort", modes: ["none", "low", "medium", "high", "xhigh"], default: "none" }, structured_output: { parameter: "response_format", types: ["json_schema"] } } },
          { id: "claude:sonnet", object: "model", airelays: { provider: "claude", reasoning: { parameter: "reasoning_effort", modes: ["low", "medium", "high", "xhigh", "max"], default: null }, structured_output: { parameter: "response_format", types: ["json_schema", "json_object"] } } },
          { id: "claude:opus", object: "model", airelays: { provider: "claude", reasoning: { parameter: "reasoning_effort", modes: ["low", "medium", "high", "xhigh", "max"], default: null }, structured_output: { parameter: "response_format", types: ["json_schema", "json_object"] } } },
        ],
      };
    case "set_custom_token":
    case "set_claude_token":
    case "submit_login_code":
    case "run_login":
    case "open_path":
      return null;
    default:
      throw new Error(`Unknown mock command: ${command}`);
  }
}
