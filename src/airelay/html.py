from __future__ import annotations

def render_home(
    *,
    relay_token_ready: bool,
    require_bearer_auth: bool,
    host: str,
    port: int,
    client_base_url: str,
    bearer_token_file: str,
    security: dict[str, object],
    providers: dict[str, object],
) -> str:
    openai_provider = dict(providers.get("openai", {}))
    claude_provider = dict(providers.get("claude", {}))
    openai_enabled = bool(openai_provider.get("enabled"))
    claude_enabled = bool(claude_provider.get("enabled"))
    openai_ready = bool(openai_provider.get("ready_for_requests"))
    claude_ready = bool(claude_provider.get("ready_for_requests"))
    provider_ready = openai_ready or claude_ready
    if openai_enabled and not claude_enabled:
        auth_state = "Upstream ChatGPT login ready" if openai_ready else "Upstream ChatGPT login missing"
    elif provider_ready:
        auth_state = "At least one provider runtime ready"
    else:
        auth_state = "No provider runtime ready"
    provider_state = "OpenAI runtime enabled"
    if claude_enabled:
        provider_state = (
            "OpenAI + Claude experimental runtime enabled"
            if openai_enabled
            else "Claude experimental runtime enabled"
        )
    if require_bearer_auth:
        token_state = "Relay client token ready" if relay_token_ready else "Relay client token missing"
        claude_setup_copy = ""
        if claude_enabled:
            claude_setup_copy = (
                "<li><code>claude auth login --claudeai</code> prepares the local Claude runtime</li>"
                "<li><code>claude setup-token</code> enables headless Claude CLI auth via "
                "<code>CLAUDE_CODE_OAUTH_TOKEN</code></li>"
            )
        if not relay_token_ready:
            next_step = "airelays init"
        elif provider_ready:
            next_step = f"airelays serve --host {host} --port {port}"
        elif openai_enabled and claude_enabled:
            next_step = "airelays login or claude auth login --claudeai"
        elif openai_enabled:
            next_step = "airelays login"
        else:
            next_step = "claude auth login --claudeai"
        client_credential_copy = (
            "Use the AIRelays bearer token as the client credential so standard "
            "OpenAI-compatible SDKs send <code>Authorization: Bearer ...</code>. "
            f"Default token file: <code>{bearer_token_file}</code>."
        )
        security_defaults = "<code>loopback + bearer token + rate limits</code>"
        security_copy = "The relay protects <code>/v1/*</code> and <code>/no-tools/v1/*</code> by default."
        surface_label = "Protected API"
        surface_value = "<code>/v1/*</code> and <code>/no-tools/v1/*</code>"
        first_run_copy = (
            "<li><code>airelays init</code> writes a config file and relay token</li>"
            + (
                "<li><code>airelays login</code> creates an AIRelays-owned OpenAI subscription session</li>"
                if openai_enabled
                else ""
            )
            + "<li><code>airelays serve --port 8080</code> launches the protected local endpoint</li>"
            + "<li><code>airelays status</code> shows config, token, and provider-readiness details</li>"
            f"{claude_setup_copy}"
        )
        protected_surface_copy = (
            "<li><code>GET /v1/models</code>, <code>POST /v1/responses</code>, and other API routes require the relay token</li>"
            f"<li>Per-IP rate limits: {security['rate_limit_per_minute']}/minute with burst {security['rate_limit_burst']}</li>"
            f"<li>Concurrent request cap: {security['concurrent_requests_per_ip']} per IP</li>"
            "<li>Repeated bad tokens trigger a temporary IP block instead of unlimited brute force</li>"
        )
        if claude_provider.get("enabled"):
            protected_surface_copy += (
                "<li>Claude experimental models are local-only, loopback-only, and stateless</li>"
                "<li>Claude experimental models support text chat and text completions only</li>"
            )
        diagnostics_copy = (
            "<li><code>GET /healthz</code> is intentionally minimal and public</li>"
            "<li><code>GET /v1/relay/status</code> returns protected config, auth, storage, and limiter state</li>"
            "<li>Uploads and conversations stay local; secrets are redacted in logs</li>"
            "<li>Use <code>airelays token show</code> to display the current client token</li>"
            "<li>Use <code>airelays token rotate</code> to issue a fresh client token</li>"
        )
    else:
        token_state = "Relay auth disabled"
        if provider_ready:
            next_step = f"airelays serve --host {host} --port {port} --no-auth"
        elif openai_enabled:
            next_step = "airelays login"
        else:
            next_step = "Complete provider login first"
        client_credential_copy = (
            "Clients can call this AIRelays base URL without <code>Authorization</code>. "
            "If your SDK insists on an <code>api_key</code> value, any non-empty placeholder string is acceptable."
        )
        security_defaults = "<code>loopback + open relay mode + rate limits</code>"
        security_copy = (
            "All <code>/v1/*</code> and <code>/no-tools/v1/*</code> routes are open in this process. "
            "Keep the listener on loopback unless you intentionally want broader access."
        )
        surface_label = "Open API"
        surface_value = "<code>/v1/*</code> and <code>/no-tools/v1/*</code>"
        first_run_copy = (
            "<li><code>airelays init --no-auth</code> writes a config file with bearer auth disabled</li>"
            "<li><code>airelays login</code> creates an AIRelays-owned OpenAI subscription session</li>"
            "<li><code>airelays serve --no-auth --port 8080</code> launches the open local endpoint</li>"
            "<li><code>airelays status</code> shows config and provider-readiness details</li>"
        )
        protected_surface_copy = (
            "<li>All documented API routes are accessible without a relay token in this process</li>"
            f"<li>Per-IP rate limits still apply: {security['rate_limit_per_minute']}/minute with burst {security['rate_limit_burst']}</li>"
            f"<li>Concurrent request cap remains {security['concurrent_requests_per_ip']} per IP</li>"
            "<li>Use loopback binding unless you intentionally want an open relay on a broader interface</li>"
        )
        diagnostics_copy = (
            "<li><code>GET /healthz</code> stays minimal and public</li>"
            "<li><code>GET /v1/relay/status</code> is also open when bearer auth is disabled</li>"
            "<li>Uploads and conversations stay local; secrets are redacted in logs</li>"
            "<li>If a client library insists on an API key field, use any non-empty placeholder value</li>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AIRelays</title>
  <style>
    :root {{
      --paper: #f4efe6;
      --ink: #102331;
      --muted: #566878;
      --accent: #dd5b38;
      --accent-soft: rgba(221, 91, 56, 0.16);
      --teal: #1d6f6d;
      --edge: rgba(16, 35, 49, 0.12);
      --panel: rgba(255, 255, 255, 0.82);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(221, 91, 56, 0.30), transparent 32%),
        radial-gradient(circle at bottom right, rgba(29, 111, 109, 0.24), transparent 28%),
        linear-gradient(160deg, #f4efe6 0%, #f7f3eb 50%, #efe7da 100%);
      padding: 28px 18px 60px;
    }}
    .shell {{
      max-width: 1120px;
      margin: 0 auto;
      display: grid;
      gap: 22px;
    }}
    .hero, .panel {{
      background: var(--panel);
      border: 1px solid var(--edge);
      border-radius: 28px;
      box-shadow: 0 18px 50px rgba(16, 35, 49, 0.12);
      backdrop-filter: blur(18px);
    }}
    .hero {{
      padding: 28px;
      display: grid;
      gap: 18px;
    }}
    .eyebrow {{
      font-family: "IBM Plex Mono", monospace;
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(40px, 7vw, 74px);
      line-height: 0.92;
      max-width: 10ch;
    }}
    p {{
      margin: 0;
      max-width: 72ch;
      color: var(--muted);
      line-height: 1.6;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 9px 14px;
      border-radius: 999px;
      font-weight: 700;
      background: var(--accent-soft);
      color: #7d2e19;
    }}
    .chip.safe {{
      background: rgba(29, 111, 109, 0.16);
      color: #174f4d;
    }}
    .grid {{
      display: grid;
      gap: 18px;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    }}
    .panel {{
      padding: 22px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 22px;
    }}
    ul {{
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.65;
    }}
    code {{
      font-family: "IBM Plex Mono", monospace;
      font-size: 0.94em;
      background: rgba(16, 35, 49, 0.08);
      padding: 2px 6px;
      border-radius: 7px;
    }}
    .actions {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }}
    .action {{
      padding: 18px;
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.84);
      border: 1px solid rgba(16, 35, 49, 0.08);
    }}
    .action strong {{
      display: block;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 10px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
    }}
    .stat {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(16, 35, 49, 0.08);
    }}
    .stat strong {{
      display: block;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .stat span {{
      font-size: 18px;
      font-weight: 700;
      word-break: break-word;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Subscription-backed AI relay</div>
      <div class="chips">
        <div class="chip">{auth_state}</div>
        <div class="chip safe">{token_state}</div>
        <div class="chip safe">{provider_state}</div>
      </div>
      <h1>AIRelays provides an OpenAI-compatible endpoint over your subscription login.</h1>
      <p>
        AIRelays stores its own subscription login, protects the relay with your own bearer token,
        and lets standard OpenAI clients talk to one local base URL. OpenAI subscription routes
        stay first-class, and optional provider adapters can expose smaller compatible subsets.
        Tool-enabled and tool-disabled routes stay separate, and every transit is logged to JSONL.
      </p>
      <p>
        AIRelays is an independent third-party project for single-user local convenience. It is
        not affiliated with or endorsed by any provider.
      </p>
      <div class="actions">
        <div class="action">
          <strong>Next Step</strong>
          <code>{next_step}</code>
          <p>Use <code>airelays init</code> when you want config plus the default protected token path, or <code>airelays init --no-auth</code> for an open local relay. Provider login depends on the enabled runtime.</p>
        </div>
        <div class="action">
          <strong>Client Base URL</strong>
          <code>{client_base_url}</code>
          <p>{client_credential_copy}</p>
        </div>
        <div class="action">
          <strong>Security Defaults</strong>
          {security_defaults}
          <p>{security_copy}</p>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><strong>Listener</strong><span>{host}:{port}</span></div>
        <div class="stat"><strong>{surface_label}</strong><span>{surface_value}</span></div>
        <div class="stat"><strong>Providers</strong><span>{provider_state}</span></div>
        <div class="stat"><strong>Public Status</strong><span><code>GET /healthz</code></span></div>
        <div class="stat"><strong>{"Protected Diagnostics" if require_bearer_auth else "Relay Diagnostics"}</strong><span><code>GET /v1/relay/status</code></span></div>
      </div>
    </section>
    <section class="grid">
      <article class="panel">
        <h2>First Run</h2>
        <ul>
          {first_run_copy}
        </ul>
      </article>
      <article class="panel">
        <h2>{surface_label}</h2>
        <ul>
          {protected_surface_copy}
        </ul>
      </article>
      <article class="panel">
        <h2>Diagnostics</h2>
        <ul>
          {diagnostics_copy}
        </ul>
      </article>
    </section>
  </main>
</body>
</html>
"""
