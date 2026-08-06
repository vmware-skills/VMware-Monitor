"""Tiny read-only vSphere Automation REST client (session-auth + GET only).

vmware-monitor is a pyVmomi (SOAP) skill; a few vSphere 9.1 read surfaces
(vLCM cluster compliance, appliance deployment size) exist *only* on the
vSphere Automation REST API. This is the smallest client that can read them
safely, reusing the same per-target credentials as the SOAP path.

Design mirrors the family's REST-wrapper connection layer (踩坑 #37): HTTP error
codes are translated *centrally* here into either an authored teaching error
(4xx — names the fix) or a "not ready" signal (5xx/timeout — the caller decides
what that means), so no ops function has to special-case ``raise_for_status``
and none leaks a raw response body to an agent.

Read-only by construction: the only non-GET request is the mandatory
``POST /api/session`` Basic-auth → session-id bootstrap. There is no method here
that can mutate vCenter state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from vmware_monitor.config import TargetConfig

# The one non-GET call: exchange Basic-auth for a session id (documented,
# long-standing vSphere Automation endpoint — see tests/eval/spec).
_SESSION_PATH = "/api/session"
_SESSION_HEADER = "vmware-api-session-id"
_TIMEOUT_S = 30.0


class RestNotReadyError(Exception):
    """vCenter answered 5xx / timed out — likely mid-patch/maintenance or booting.

    vSphere 9.1 exposes *no* maintenance-status endpoint (the ``X-VC-Maintenance``
    headers are a hallucination — spec §D); a 503 is the only signal, and it is a
    transient *state*, not a fault. Callers translate this into a structured
    "not available, no ETA" result rather than an error, so a health read degrades
    instead of crashing when the platform is busy being patched.
    """


class RestAuthError(ValueError):
    """Credential/permission failure with an authored, leak-free fix hint.

    Subclasses ``ValueError`` so the MCP ``_safe_error`` allowlist passes the
    teaching text through verbatim (the same treatment the SOAP ConfigError gets),
    while the raw upstream body — which can quote internal detail — never travels.
    """


class RestNotFoundError(ValueError):
    """A templated id (e.g. cluster MoID) did not resolve — names how to get one."""


def _base_url(target: TargetConfig) -> str:
    return f"https://{target.host}:{target.port}"


def _translate_status(exc: httpx.HTTPStatusError, path: str) -> Exception:
    """Map an HTTP error status to the right authored exception (no body leak)."""
    code = exc.response.status_code
    if code in (502, 503, 504):
        return RestNotReadyError(
            f"vCenter returned {code} for {path}. It is likely mid-patch or "
            "restarting; vSphere exposes no maintenance-ETA endpoint, so retry "
            "shortly."
        )
    if code in (401, 403):
        return RestAuthError(
            f"vCenter refused the REST request to {path} ({code}). Check the "
            "target's username/password in ~/.vmware-monitor/.env and that the "
            "account has read access, then run 'vmware-monitor doctor'."
        )
    if code == 404:
        return RestNotFoundError(
            f"vCenter has no resource at {path} (404). If this used a cluster id, "
            "confirm it with 'vmware-monitor inventory clusters' — the REST API "
            "wants the cluster MoID (e.g. domain-c123), not its display name."
        )
    return RestAuthError(f"vCenter REST call to {path} failed ({code}).")


class VsphereRest:
    """Per-target read-only REST session with a cached session id."""

    def __init__(self, target: TargetConfig) -> None:
        self._target = target
        self._base = _base_url(target)
        self._verify = target.verify_ssl
        self._session_id: str | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self._base, verify=self._verify, timeout=_TIMEOUT_S)

    def _login(self) -> str:
        """POST /api/session with Basic auth → session id (cached).

        ``target.password`` may raise ConfigError (missing env var); that is an
        authored, leak-free message and is allowed to propagate unchanged.
        """
        auth = httpx.BasicAuth(self._target.username, self._target.password)
        try:
            with self._client() as client:
                resp = client.post(_SESSION_PATH, auth=auth)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _translate_status(exc, _SESSION_PATH) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RestNotReadyError(
                f"Could not reach vCenter REST endpoint at {self._base} "
                f"({type(exc).__name__}). Verify the host is up and run "
                "'vmware-monitor doctor'."
            ) from exc
        token = resp.json()
        if not isinstance(token, str) or not token:
            raise RestAuthError(
                f"vCenter session bootstrap at {self._base} returned no session id."
            )
        self._session_id = token
        return token

    def get_json(self, path: str) -> Any:
        """Authenticated GET → parsed JSON. One re-auth retry on a stale 401.

        Transient transport/5xx failures surface as :class:`RestNotReadyError`; 4xx as
        an authored teaching error. Never returns a raw upstream body to the caller.
        """
        token = self._session_id or self._login()
        try:
            return self._get_once(path, token)
        except RestAuthError:
            # Session may have expired between calls — re-auth once, then give up.
            token = self._login()
            return self._get_once(path, token)

    def _get_once(self, path: str, token: str) -> Any:
        headers = {_SESSION_HEADER: token}
        try:
            with self._client() as client:
                resp = client.get(path, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _translate_status(exc, path) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RestNotReadyError(
                f"vCenter REST GET {path} could not complete "
                f"({type(exc).__name__}); retry shortly."
            ) from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise RestAuthError(
                f"vCenter REST GET {path} returned a non-JSON body."
            ) from exc
