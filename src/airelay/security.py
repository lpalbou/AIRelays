from __future__ import annotations

import hashlib
import ipaddress
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from airelay.config import Settings
from airelay.traffic import TrafficLogger
from airelay.errors import error_body


@dataclass(slots=True)
class AccessLease:
    request_id: str
    client_ip: str
    protected: bool
    released: bool = False


@dataclass(slots=True)
class ClientState:
    allowance: float
    last_refill: float
    active_requests: int = 0
    blocked_until: float = 0.0
    failed_auth_timestamps: list[float] = field(default_factory=list)
    last_seen: float = 0.0


def _token_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# "private" scope: the address ranges a home, office, or VPN network uses.
# Spelled out instead of `ipaddress.is_private`, which also covers
# documentation and benchmarking ranges (e.g. 203.0.113.0/24) that can be
# routed on the internet in practice.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",  # loopback
        "10.0.0.0/8",  # RFC 1918
        "172.16.0.0/12",  # RFC 1918
        "192.168.0.0/16",  # RFC 1918
        "100.64.0.0/10",  # CGNAT shared space (Tailscale)
        "169.254.0.0/16",  # IPv4 link-local
        "::1/128",  # IPv6 loopback
        "fc00::/7",  # IPv6 unique local
        "fe80::/10",  # IPv6 link-local
    )
)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    # An IPv4 client on a dual-stack listener arrives as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def address_in_scope(value: str, scope: str) -> bool:
    """Whether a client address may connect under `scope` ("loopback",
    "private" or "any"). Unparsable addresses are out of every narrow scope."""
    if scope == "any":
        return True
    address = _parse_ip(value)
    if address is None:
        return False
    if scope == "loopback":
        return address.is_loopback
    return any(address in network for network in _PRIVATE_NETWORKS if network.version == address.version)


def _error_response(
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_body(message, status_code=status_code, code=code, type_=error_type),
        status_code=status_code,
        headers=headers,
    )


class EndpointProtector:
    def __init__(self, settings: Settings, traffic: TrafficLogger) -> None:
        self._settings = settings
        self._traffic = traffic
        self._lock = threading.Lock()
        self._clients: dict[str, ClientState] = {}

    def is_protected_path(self, path: str) -> bool:
        return path.startswith("/v1/") or path.startswith("/no-tools/v1/")

    def client_ip(self, request: Request) -> str:
        return self._client_address(request)[0]

    def _client_address(self, request: Request) -> tuple[str, bool]:
        """(address, from_network): from_network is False only when the
        transport has no network peer at all (a Unix socket or an
        in-process ASGI client)."""
        if self._settings.trust_x_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                first = forwarded.split(",", 1)[0].strip()
                if first:
                    return first, True
        client = request.client
        if client and client.host:
            # A TCP peer is always an IP literal; anything else is not a
            # network client (e.g. Starlette's "testclient").
            return client.host, _parse_ip(client.host) is not None
        return "unknown", False

    def _scope_rejection(self, request_id: str, request: Request) -> Response | None:
        """Enforce `network_scope` before rate limits and auth, so a client
        outside the allowed networks cannot even probe the API key."""
        scope = self._settings.effective_network_scope()
        if scope == "any":
            return None
        address, from_network = self._client_address(request)
        if not from_network or address_in_scope(address, scope):
            return None
        if scope == "loopback":
            code = "client_not_on_this_machine"
            message = "This relay only accepts connections from this machine (network_scope = \"loopback\")."
        else:
            code = "client_not_on_private_network"
            message = (
                "This relay only accepts connections from private networks "
                "(network_scope = \"private\"); this client address is public."
            )
        return self._reject(
            request_id=request_id,
            client_ip=address,
            status_code=403,
            error_type="permission_error",
            code=code,
            message=message,
            reason="network_scope",
        )

    def acquire(self, request_id: str, request: Request) -> tuple[AccessLease, Response | None]:
        client_ip = self.client_ip(request)
        lease = AccessLease(
            request_id=request_id,
            client_ip=client_ip,
            protected=self.is_protected_path(request.url.path),
        )
        # Every path, not only the API: in a narrow scope an outside client
        # must not see the home page or probe /healthz either.
        rejection = self._scope_rejection(request_id, request)
        if rejection is not None:
            lease.released = True  # No concurrency slot was acquired.
            return lease, rejection
        if not lease.protected:
            return lease, None

        now = time.monotonic()
        with self._lock:
            state = self._client_state(client_ip, now)
            self._refill(state, now)
            if state.blocked_until > now:
                retry_after = max(1, math.ceil(state.blocked_until - now))
                return lease, self._reject(
                    request_id=request_id,
                    client_ip=client_ip,
                    status_code=429,
                    error_type="rate_limit_error",
                    code="ip_temporarily_blocked",
                    message="Too many invalid authentication attempts from this IP.",
                    reason="auth_block_active",
                    retry_after=retry_after,
                )
            # The lightweight tray counter must remain available while a
            # query occupies the last slot, and must not spend query quota.
            # Only authenticated GETs qualify; invalid credentials continue
            # through the normal rate limits and failed-auth protection.
            if (
                request.method == "GET"
                and request.url.path == "/v1/relay/status"
                and request.query_params.get("activity_only") == "true"
            ):
                expected = self._settings.resolve_bearer_token()
                presented = self._presented_bearer_token(request)
                if not self._settings.require_bearer_auth or (
                    expected and presented and secrets.compare_digest(expected, presented)
                ):
                    lease.released = True  # No concurrency slot was acquired.
                    return lease, None
            if state.active_requests >= self._settings.concurrent_requests_per_ip:
                return lease, self._reject(
                    request_id=request_id,
                    client_ip=client_ip,
                    status_code=429,
                    error_type="rate_limit_error",
                    code="too_many_concurrent_requests",
                    message="Too many concurrent requests from this IP.",
                    reason="concurrency_limit",
                    retry_after=1,
                )
            if state.allowance < 1.0:
                return lease, self._reject(
                    request_id=request_id,
                    client_ip=client_ip,
                    status_code=429,
                    error_type="rate_limit_error",
                    code="rate_limit_exceeded",
                    message="Request rate limit exceeded for this IP.",
                    reason="request_rate_limit",
                    retry_after=1,
                )
            state.allowance -= 1.0
            state.active_requests += 1

        if not self._settings.require_bearer_auth:
            return lease, None

        expected_token = self._settings.resolve_bearer_token()
        presented_token = self._presented_bearer_token(request)
        if not expected_token or not presented_token or not secrets.compare_digest(
            expected_token, presented_token
        ):
            self._register_auth_failure(lease, request, presented_token)
            return lease, _error_response(
                status_code=401,
                message="Missing or invalid AIRelays bearer token.",
                error_type="authentication_error",
                code="invalid_api_key",
            )

        return lease, None

    def release(self, lease: AccessLease) -> None:
        if lease.released or not lease.protected:
            return
        now = time.monotonic()
        with self._lock:
            state = self._clients.get(lease.client_ip)
            if state is not None:
                state.active_requests = max(0, state.active_requests - 1)
                state.last_seen = now
                self._gc_locked(lease.client_ip, state, now)
        lease.released = True

    def summary(self) -> dict[str, Any]:
        return {
            "protected_routes": ["/v1/*", "/no-tools/v1/*"],
            "require_bearer_auth": self._settings.require_bearer_auth,
            "network_scope": self._settings.effective_network_scope(),
            "bearer_token_present": bool(self._settings.resolve_bearer_token()),
            "rate_limit_per_minute": self._settings.rate_limit_per_minute,
            "rate_limit_burst": self._settings.rate_limit_burst,
            "concurrent_requests_per_ip": self._settings.concurrent_requests_per_ip,
            "failed_auth_window_seconds": self._settings.failed_auth_window_seconds,
            "failed_auth_max_attempts": self._settings.failed_auth_max_attempts,
            "failed_auth_block_seconds": self._settings.failed_auth_block_seconds,
        }

    def diagnostics(self, client_ip: str | None = None) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            tracked = len(self._clients)
            blocked = 0
            active = 0
            client_state = self._clients.get(client_ip) if client_ip else None
            for state in self._clients.values():
                if state.blocked_until > now:
                    blocked += 1
                if state.active_requests > 0:
                    active += 1
            payload = {
                **self.summary(),
                "tracked_clients": tracked,
                "blocked_clients": blocked,
                "active_clients": active,
            }
            if client_state is not None and client_ip is not None:
                payload["client"] = {
                    "ip": client_ip,
                    "allowance_remaining": round(client_state.allowance, 2),
                    "active_requests": client_state.active_requests,
                    "failed_auth_attempts": len(client_state.failed_auth_timestamps),
                    "blocked_for_seconds": max(
                        0, math.ceil(client_state.blocked_until - now)
                    ),
                }
        return payload

    def _client_state(self, client_ip: str, now: float) -> ClientState:
        state = self._clients.get(client_ip)
        if state is None:
            state = ClientState(
                allowance=float(self._settings.rate_limit_burst),
                last_refill=now,
                last_seen=now,
            )
            self._clients[client_ip] = state
        return state

    def _refill(self, state: ClientState, now: float) -> None:
        elapsed = max(0.0, now - state.last_refill)
        refill_per_second = self._settings.rate_limit_per_minute / 60.0
        state.allowance = min(
            float(self._settings.rate_limit_burst),
            state.allowance + (elapsed * refill_per_second),
        )
        state.last_refill = now
        state.last_seen = now

    def _presented_bearer_token(self, request: Request) -> str | None:
        header = request.headers.get("authorization")
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = value.strip()
        return token or None

    def _register_auth_failure(
        self,
        lease: AccessLease,
        request: Request,
        presented_token: str | None,
    ) -> None:
        now = time.monotonic()
        blocked = False
        block_for = 0
        with self._lock:
            state = self._client_state(lease.client_ip, now)
            state.active_requests = max(0, state.active_requests - 1)
            cutoff = now - self._settings.failed_auth_window_seconds
            state.failed_auth_timestamps = [
                ts for ts in state.failed_auth_timestamps if ts >= cutoff
            ]
            state.failed_auth_timestamps.append(now)
            if len(state.failed_auth_timestamps) >= self._settings.failed_auth_max_attempts:
                state.blocked_until = now + self._settings.failed_auth_block_seconds
                blocked = True
                block_for = self._settings.failed_auth_block_seconds
            self._gc_locked(lease.client_ip, state, now)
        lease.released = True
        self._traffic.write(
            {
                "request_id": lease.request_id,
                "phase": "endpoint_auth_failed",
                "method": request.method,
                "path": request.url.path,
                "client_ip": lease.client_ip,
                "headers": dict(request.headers.items()),
                "token_fingerprint": _token_fingerprint(presented_token),
                "blocked": blocked,
                "block_seconds": block_for if blocked else 0,
            }
        )

    def _reject(
        self,
        request_id: str,
        client_ip: str,
        status_code: int,
        error_type: str,
        code: str,
        message: str,
        reason: str,
        retry_after: int | None = None,
    ) -> JSONResponse:
        entry: dict[str, Any] = {
            "request_id": request_id,
            "phase": "endpoint_rejected",
            "client_ip": client_ip,
            "reason": reason,
            "status_code": status_code,
        }
        if retry_after is not None:
            entry["retry_after_seconds"] = retry_after
        self._traffic.write(entry)
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        return _error_response(status_code, message, error_type, code, headers=headers)

    def _gc_locked(self, client_ip: str, state: ClientState, now: float) -> None:
        idle_seconds = max(
            self._settings.failed_auth_window_seconds,
            self._settings.failed_auth_block_seconds,
            3600,
        )
        if state.active_requests > 0:
            return
        if state.blocked_until > now:
            return
        if now - state.last_seen < idle_seconds:
            return
        if state.failed_auth_timestamps:
            return
        self._clients.pop(client_ip, None)
