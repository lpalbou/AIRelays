# Completed: Internet access scope with a mandatory API key

## Metadata

- Created: 2026-10-25
- Status: Completed
- Completed: 2026-10-25

## Context

The dashboard offered **This machine only** or **Devices on my network**.
The second bound the relay to `0.0.0.0`, and the relay did not filter client
addresses: "my network" held only because the router's NAT hid the port.
A forwarded port, a UPnP rule or a public IPv6 address exposed the relay to
the internet without any warning, possibly with open auth.

## What we did

- Relay: `[security] network_scope` (`AIRELAYS_NETWORK_SCOPE`,
  `airelays serve --network-scope`): `loopback`, `private` (default) or
  `any`, validated like the balancing strategies (a typo fails at load). A
  loopback listener always means `loopback`.
- `EndpointProtector` checks the scope before rate limits and auth, on
  every route: `private` admits loopback, RFC 1918, `100.64.0.0/10`
  (CGNAT/Tailscale), IPv4 link-local, IPv6 ULA and link-local, and their
  IPv4-mapped forms (explicit list, not `ipaddress.is_private`, which also
  covers routable documentation ranges). Others get `403`
  `client_not_on_private_network` (`client_not_on_this_machine` for
  `loopback`), logged as `endpoint_rejected`. With trusted
  `X-Forwarded-For`, the first forwarded address is checked.
- `network_scope = "any"` requires `require_bearer_auth = true`: `serve`,
  `init`, `doctor` and `create_app` refuse the config otherwise, and the
  desktop's settings validation applies the same rule.
- Scope in `airelays status` / `/v1/relay/status` (`relay.network_scope`,
  `relay.effective_network_scope`, `security.network_scope`).
- Desktop: `networkScope` setting (old settings files load as `private`),
  rendered into the relay config; `set_network_scope` replaces
  `set_network_exposure` (kept as a shim). Choosing `any` turns the API key
  on in the same settings change; open auth while `any` is refused by the
  backend, and a refused change leaves the saved settings untouched.
- Dashboard: **Anyone on the internet** opens an inline confirmation
  (port forwarding, quota exposure, plain HTTP without TLS, mandatory key and
  per-IP limits) and applies only on **Enable internet access**. While
  active, **Open (no key)** is disabled with an explanation, a warning stays
  visible, and the address list notes that the public address depends on
  the router.

## Validation

- `pytest`: new `tests/test_network_scope.py` (private ranges accepted,
  public addresses rejected before auth, `any` accepts public clients and
  refuses open auth in config, `serve` and `create_app`, X-Forwarded-For
  interplay, config round trip and typo rejection, status payload).
- `cargo test`: settings default, validation and rendering of
  `network_scope`.
- Browser test: the internet flow (confirmation, cancel, confirm forces the
  key, open mode disabled and refused) at 1000 and 760 px.

## Follow-ups

- TLS termination stays out of scope: the docs point to a reverse proxy or
  a VPN.
