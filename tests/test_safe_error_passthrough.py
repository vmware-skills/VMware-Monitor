"""A teaching message the agent never sees is not a teaching message.

``_safe_error`` reduces unrecognised exceptions to ``"<Class>: operation
failed."`` so raw vSphere text cannot leak. Its allowlist held only the builtin
validation errors, so all three exceptions this skill defines for its own domain
— ``VMNotFoundError``, ``HostNotFoundError``, ``DatastoreNotFoundError`` — had
their messages replaced by their class names on the way to the agent.

Those three cover the most common failure this skill has: a name that does not
resolve. They exist precisely to say which listing tool produces a valid name,
and that sentence was being thrown away at the last step. The CLI printed it in
full and the error-quality eval read it at the raise site, so nothing measured
what actually arrived.

The same defect existed one layer earlier for the missing-password error — this
family's most common *first-run* failure, whose whole remedy is the ``.env`` path
and env var name it carries. An agent hitting an unconfigured target received
``OSError: operation failed.`` and had nothing to act on.

Admitting bare ``OSError`` fixed that and opened a wider door than intended.
``sanitize`` strips control characters and truncates; it does not redact. So
``socket.gaierror`` (the name that failed to resolve) and connection errors
carrying a full ``scheme://host:port/path`` reached the agent verbatim through
that entry. The narrow :class:`~vmware_monitor.config.ConfigError` carries the
messages that needed to pass, and nothing else.

Narrowing alone is not sufficient, and this is the part an allowlist cannot
express. ``ssl.SSLCertVerificationError`` subclasses **both** ``OSError`` and
``ValueError``, and ``ValueError`` has been on this list since long before
``OSError`` was — so the certificate subject went on arriving however narrow the
``OSError`` side became. A list of types that may pass has no way to say "except
this one"; the reduction has to run *ahead* of it.

What replaces the raw text is the connection layer's authored
:class:`~vmware_monitor.connection.ConnectError`, which names the target as
config.yaml spells it and its ``verify_ssl`` setting — never the resolved host,
the port, or the certificate.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from vmware_monitor.config import ConfigError, TargetConfig
from vmware_monitor.connection import ConnectError, ConnectionManager, _unreachable_reason
from vmware_monitor.mcp_server.server import _safe_error
from vmware_monitor.ops.investigate_datastore import DatastoreNotFoundError
from vmware_monitor.ops.investigate_host import HostNotFoundError
from vmware_monitor.ops.vm_info import VMNotFoundError

TEACHING = "VM 'web-99' not found. Run list_virtual_machines and copy an exact name."

ENV_KEY = "VMWARE_VCENTER_PROD_PASSWORD"

#: Cap applied by ``_safe_error`` before the message reaches the agent.
CAP = 300

#: A real handshake failure quotes both of these. Neither may reach an agent.
CERT_HOST = "vc-internal.corp.example.com"
CERT_TEXT = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
    f"certificate in certificate chain, subject CN={CERT_HOST} (_ssl.c:1006)"
)


def _missing_password(name: str = "vcenter-prod") -> ConfigError:
    """The real message, raised by the real property — not a copy of its text."""
    target = TargetConfig(name=name, host="vc.example.com", config_username="u")
    with pytest.raises(ConfigError) as excinfo:
        target.password
    return excinfo.value


# ── the narrow passthrough ──────────────────────────────────────────────────


def test_missing_password_keeps_the_env_var_name():
    """The one config error raised on purpose — and the whole point of it is the name."""
    out = _safe_error(_missing_password(), "list_virtual_machines")
    assert ENV_KEY in out
    assert "operation failed" not in out


@pytest.mark.parametrize(
    "exc_type", [VMNotFoundError, HostNotFoundError, DatastoreNotFoundError]
)
def test_domain_exceptions_keep_their_message(exc_type):
    assert _safe_error(exc_type(TEACHING), "get_vm_info") == TEACHING


@pytest.mark.parametrize("exc_type", [ValueError, FileNotFoundError, KeyError, PermissionError])
def test_validation_errors_still_pass_through(exc_type):
    assert "web-99" in _safe_error(exc_type(TEACHING), "t")


def test_dropped_connection_surfaces_its_hint():
    assert "retry" in _safe_error(ConnectionError("Connection lost — retry the operation."), "t")


# ── what the narrow type keeps out ──────────────────────────────────────────


def test_dns_failure_does_not_leak_the_hostname():
    """``socket.gaierror``'s only base is OSError, so this is what narrowing buys."""
    exc = socket.gaierror(
        "[Errno 8] nodename nor servname provided, or not known: vcenter-lab.corp.internal"
    )
    out = _safe_error(exc, "list_virtual_machines")
    assert out == "gaierror: operation failed."
    assert "vcenter-lab.corp.internal" not in out


def test_a_bare_oserror_is_no_longer_a_passthrough():
    """The guard, stated directly: OSError-ness alone buys nothing."""
    out = _safe_error(OSError("connect to 10.20.30.40:443 failed"), "t")
    assert out == "OSError: operation failed."
    assert "10.20.30.40" not in out


def test_tls_failure_does_not_leak_the_certificate_subject():
    """Reduced by the pre-check, not by the allowlist — it is also a ValueError.

    Only the ``isinstance`` branches of ``_safe_error`` are under test, so the
    exception is constructed rather than provoked through a real handshake.
    """
    out = _safe_error(ssl.SSLCertVerificationError(CERT_TEXT), "list_virtual_machines")
    assert out == "SSLCertVerificationError: operation failed."
    assert CERT_HOST not in out


def test_unplanned_exceptions_are_still_reduced():
    """The redaction this allowlist exists for has to keep working."""
    out = _safe_error(RuntimeError("https://admin:hunter2@vc.internal/api/task-42"), "t")
    assert out == "RuntimeError: operation failed."
    assert "hunter2" not in out


def test_message_is_still_truncated():
    assert len(_safe_error(VMNotFoundError("x" * 900), "t")) <= CAP


# ── what replaces it: the connection layer's authored message ───────────────


def _lab_target(**overrides) -> TargetConfig:
    kwargs = dict(
        name="lab-vc",
        host=CERT_HOST,
        config_username="svc@vsphere.local",
        port=8443,
        verify_ssl=True,
    )
    kwargs.update(overrides)
    return TargetConfig(**kwargs)


def test_a_transport_failure_teaches_without_naming_the_host(monkeypatch):
    """The diagnostic must not simply be dropped — reducing to a class name tells
    an operator nothing, and self-signed certs are this family's usual cause."""
    import pyVim.connect as pvc

    def _boom(**kwargs):
        raise ssl.SSLCertVerificationError(CERT_TEXT)

    monkeypatch.setenv("VMWARE_LAB_VC_PASSWORD", "pw")
    monkeypatch.setattr(pvc, "SmartConnect", _boom)

    with pytest.raises(ConnectError) as excinfo:
        ConnectionManager._create_connection(_lab_target())

    out = _safe_error(excinfo.value, "list_virtual_machines")
    assert len(out) <= CAP
    # Names what the operator can act on...
    assert "lab-vc" in out
    assert "verify_ssl" in out
    assert "vmware-monitor doctor" in out
    # ...and nothing the raw exception would have leaked.
    assert CERT_HOST not in out
    assert "8443" not in out
    assert "CERTIFICATE_VERIFY_FAILED" not in out


def test_a_missing_password_is_not_answered_with_a_tls_remedy(monkeypatch):
    """``target.password`` is a property that raises inside the argument list.

    Evaluated inside the wrapped call, the family's most common first-run
    failure would be caught by the connection-failure handler — an OSError
    subclass like any other — and answered with a remedy about certificates.
    """
    monkeypatch.delenv("VMWARE_LAB_VC_PASSWORD", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        ConnectionManager._create_connection(_lab_target())

    assert not isinstance(excinfo.value, ConnectError)
    out = _safe_error(excinfo.value, "list_virtual_machines")
    assert "VMWARE_LAB_VC_PASSWORD" in out
    assert "verify_ssl" not in out


def test_credentials_still_resolve_as_a_pair(monkeypatch):
    """Reading them into locals must not split the pair v1.8.3 exists to keep whole."""
    import pyVim.connect as pvc

    seen: dict[str, str] = {}

    def _capture(**kwargs):
        seen.update(user=kwargs["user"], pwd=kwargs["pwd"])
        return object()

    monkeypatch.setattr(pvc, "SmartConnect", _capture)
    monkeypatch.setenv("VMWARE_LAB_VC_USERNAME", "svc-b@vsphere.local")
    monkeypatch.setenv("VMWARE_LAB_VC_PASSWORD", "pw-b")

    ConnectionManager._create_connection(_lab_target())
    assert seen == {"user": "svc-b@vsphere.local", "pwd": "pw-b"}


def test_the_cross_vcenter_rollup_still_names_the_transport_failure():
    """``unreachable`` shows class names only, so the class name has to mean something.

    Reporting "ConnectError" for every dead target would replace a usable
    diagnosis with the name of our own wrapper.
    """
    wrapped = ConnectError("authored text", cause_name="SSLCertVerificationError")
    assert _unreachable_reason(wrapped) == "SSLCertVerificationError"
    assert _unreachable_reason(TimeoutError("x")) == "TimeoutError"
