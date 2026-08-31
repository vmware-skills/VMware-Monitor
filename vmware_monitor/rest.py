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

from vmware_policy import sanitize
from vmware_policy.compat import Requires, version_remedy

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


#: Distinguishes "not probed yet" from "probed, and unreadable". Collapsing the
#: two would re-probe an unanswering appliance on every 404.
_UNPROBED = object()

#: See tests/eval/spec/vsphere91_endpoints.py — observed on a live 8.0.3.
_VERSION_PATH = "/api/appliance/system/version"


class RestNotFoundError(ValueError):
    """A templated id (e.g. cluster MoID) did not resolve — names how to get one."""


def _base_url(target: TargetConfig) -> str:
    return f"https://{target.host}:{target.port}"


def _vcenter_explanation(exc: httpx.HTTPStatusError) -> str | None:
    """vCenter's own sentence about this error, if it wrote one.

    The vSphere Automation REST API returns a structured body on failure::

        {"error_type": "NOT_FOUND",
         "messages": [{"default_message":
             "The last remediation results for entity mgmt-cl01 are unavailable."}]}

    That sentence is the actual answer -- the cluster has never been remediated
    -- and it was being thrown away in favour of "if this used a cluster id,
    confirm it". The operator was sent to re-check an id that was correct while
    vCenter's explanation sat unread in the response.

    This is not a raw-body passthrough, which this layer deliberately does not
    do. One documented field is read, sanitised and length-capped; anything else
    in the body is ignored, and an unparseable body yields None.
    """
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 -- a non-JSON body simply has nothing to say
        return None
    if not isinstance(body, dict):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        text = entry.get("default_message")
        if isinstance(text, str) and text.strip():
            return sanitize(text.strip(), max_len=300)
    return None


def _translate_status(
    exc: httpx.HTTPStatusError,
    path: str,
    requires: Requires | None = None,
    detected: str | None = None,
) -> Exception:
    """Map an HTTP error status to the right authored exception (no body leak).

    ``requires`` names the oldest vCenter a call site works on. It only ever
    affects a 404, and only when the floor is not met: a 404 from an endpoint
    that does not exist on this vCenter is not a bad cluster MoID, and telling
    the operator to go re-check the MoID sends them after something that was
    never wrong.
    """
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
        # `detected` is the appliance's own version when it could be read, and
        # None when it could not. version_remedy() words those two differently
        # and says nothing at all when the floor is already met, so a 9.1 box
        # that 404s still gets the ordinary "check the id" remedy.
        if requires is not None:
            explained = version_remedy(requires, detected)
            if explained:
                return RestNotFoundError(f"{explained} Failing call: {path}")
        # vCenter often explains its own 404 better than any guess we can make
        # ("...are unavailable" means never remediated, not a bad id). Its
        # sentence goes first; the id advice stays as the fallback for the case
        # it really is a bad MoID.
        said = _vcenter_explanation(exc)
        if said:
            # Keep the MoID pointer on cluster-scoped paths. vCenter's sentence is
            # the better explanation for "these results are unavailable", but for
            # someone who passed a cluster's display name it names the id back at
            # them without saying what shape it should have been — so the two are
            # additive, not alternatives.
            hint = (
                " If you passed a cluster's display name, this API wants its MoID "
                "(e.g. domain-c123) — 'vmware-monitor inventory clusters' lists them."
                if "/clusters/" in path
                else ""
            )
            return RestNotFoundError(f"vCenter says: {said} (404 at {path}).{hint}")
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
        self._product_version: str | None | object = _UNPROBED

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

    def product_version(self) -> str | None:
        """Best-effort vCenter version string, or ``None`` if unreadable.

        Called only while a 404 is being turned into a message, which sets three
        rules. It must never raise, or it would replace a useful error with a
        confusing one. It must never recurse: it calls ``_get_once`` with no
        ``requires``, so its own failure cannot re-enter this path. And it must
        cache its failure as well as its success, or an appliance that cannot
        answer gets re-probed on every subsequent 404.

        ``None`` is a supported answer, not a fallback to "old" — see
        ``vmware_policy.compat.version_remedy``, which words the two differently.
        """
        if self._product_version is not _UNPROBED:
            return self._product_version

        self._product_version = None  # cache the failure before probing
        try:
            token = self._session_id or self._login()
            data = self._get_once(_VERSION_PATH, token)
        except Exception:  # noqa: BLE001 — unreadable is a supported answer
            return None
        if isinstance(data, dict):
            value = data.get("version")
            if isinstance(value, str) and value.strip():
                self._product_version = value.strip()
        return self._product_version

    def get_json(self, path: str, *, requires: Requires | None = None) -> Any:
        """Authenticated GET → parsed JSON. One re-auth retry on a stale 401.

        Transient transport/5xx failures surface as :class:`RestNotReadyError`; 4xx as
        an authored teaching error. Never returns a raw upstream body to the caller.
        """
        token = self._session_id or self._login()
        try:
            return self._get_once(path, token, requires)
        except RestAuthError:
            # Session may have expired between calls — re-auth once, then give up.
            token = self._login()
            return self._get_once(path, token, requires)

    def _get_once(self, path: str, token: str, requires: Requires | None = None) -> Any:
        headers = {_SESSION_HEADER: token}
        try:
            with self._client() as client:
                resp = client.get(path, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detected = self.product_version() if requires is not None else None
            raise _translate_status(exc, path, requires, detected) from exc
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
