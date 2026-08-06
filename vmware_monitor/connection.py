"""Connection management for vCenter and ESXi hosts.

Handles multi-target connections via pyVmomi with session reuse.
"""

from __future__ import annotations

import atexit
import ssl
from typing import TYPE_CHECKING

from pyVmomi import vim

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

from vmware_monitor.config import CONFIG_FILE, AppConfig, ConfigError, TargetConfig, load_config


class ConnectError(ConfigError):
    """A session could not be opened — with an authored, leak-free explanation.

    ``SmartConnect`` reports a refused connection through the transport layer,
    and that text names the resolved host and port, and for a TLS failure the
    certificate subject too. ``connect_all`` has always reduced such failures to
    a class name for exactly that reason; the single-target path handed the raw
    string to whoever called it, which since v1.8.4 meant straight to the agent.

    Carrying :attr:`cause_name` keeps the roll-up honest: "ConfigError" tells an
    operator nothing, while "SSLCertVerificationError" or "gaierror" says which
    knob to reach for. Only the class name of the original travels — never its
    message.
    """

    def __init__(self, message: str, cause_name: str = "") -> None:
        super().__init__(message)
        self.cause_name = cause_name


def _connect_failed(target: TargetConfig, exc: BaseException) -> ConnectError:
    """Authored replacement for a transport error, safe to show an agent.

    Names the target as it is written in config.yaml, its current ``verify_ssl``
    setting, and the file to edit — and interpolates nothing from ``exc``, whose
    text is the thing being withheld. The original survives as ``__cause__`` for
    the server-side log.
    """
    return ConnectError(
        f"Could not open a session to target '{target.name}' (its config says "
        f"verify_ssl: {str(target.verify_ssl).lower()}). Check that target's host "
        f"and port in {CONFIG_FILE}; a self-signed certificate needs "
        f"verify_ssl: false. Then run 'vmware-monitor doctor'.",
        cause_name=type(exc).__name__,
    )


def _unreachable_reason(exc: BaseException) -> str:
    """Class name for the cross-vCenter roll-up — never message text.

    :class:`ConnectError` is this module's own replacement for a transport
    failure, so its class name carries no diagnosis; report the failure it
    replaced instead, which is what distinguishes a certificate problem from a
    name that does not resolve.
    """
    if isinstance(exc, ConnectError) and exc.cause_name:
        return exc.cause_name
    return type(exc).__name__


class ConnectionManager:
    """Manages connections to multiple vCenter/ESXi targets."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connections: dict[str, ServiceInstance] = {}

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> ConnectionManager:
        cfg = config or load_config()
        return cls(cfg)

    def connect(self, target_name: str | None = None) -> ServiceInstance:
        """Connect to a target by name, or the default target."""
        target = (
            self._config.get_target(target_name)
            if target_name
            else self._config.default_target
        )

        if target.name in self._connections:
            si = self._connections[target.name]
            try:
                # Probe liveness; expired tokens can surface as a None
                # currentSession instead of raising.
                alive = si.content.sessionManager.currentSession is not None
            except Exception:
                # Any failure (NotAuthenticated, socket error, …) means the
                # cached session is unusable — drop it and reconnect below.
                alive = False
            if alive:
                return si
            del self._connections[target.name]

        si = self._create_connection(target)
        self._connections[target.name] = si
        return si

    def disconnect(self, target_name: str) -> None:
        """Disconnect from a specific target."""
        if target_name in self._connections:
            from pyVim.connect import Disconnect

            Disconnect(self._connections[target_name])
            del self._connections[target_name]

    def disconnect_all(self) -> None:
        """Disconnect from all targets."""
        for name in list(self._connections):
            self.disconnect(name)

    def list_targets(self) -> list[str]:
        """List all configured target names."""
        return [t.name for t in self._config.targets]

    def connect_all(self) -> tuple[list[tuple[str, ServiceInstance]], list[tuple[str, str]]]:
        """Connect to every configured target, tolerating per-target failures.

        Returns ``(sessions, unreachable)`` where ``sessions`` is ``[(name, si)]``
        for targets that connected and ``unreachable`` is ``[(name, reason)]`` for
        those that did not — so a cross-vCenter view degrades gracefully (one dead
        vCenter never sinks the whole roll-up) instead of failing wholesale. The
        reason is class-name only, so no host:port or credential detail leaks.
        """
        sessions: list[tuple[str, ServiceInstance]] = []
        unreachable: list[tuple[str, str]] = []
        for name in self.list_targets():
            try:
                sessions.append((name, self.connect(name)))
            except Exception as e:  # noqa: BLE001 — any connect failure degrades to "unreachable"
                unreachable.append((name, _unreachable_reason(e)))
        return sessions, unreachable

    def list_connected(self) -> list[str]:
        """List currently connected target names."""
        return list(self._connections.keys())

    @staticmethod
    def _create_connection(target: TargetConfig) -> ServiceInstance:
        """Create a new pyVmomi connection."""
        from pyVim.connect import Disconnect, SmartConnect

        context = None
        if not target.verify_ssl:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        # Resolved before the try: a missing password raises ConfigError, which
        # is an OSError subclass, and it must not be mistaken below for a
        # transport failure and rewritten into a message about certificates.
        user, pwd = target.username, target.password

        try:
            si = SmartConnect(
                host=target.host,
                user=user,
                pwd=pwd,
                port=target.port,
                sslContext=context,
                disableSslCertValidation=not target.verify_ssl,
            )
        except OSError as exc:
            # TLS, DNS and socket failures only — every one of them stringifies
            # with the host, the port, or the certificate subject. Authentication
            # faults are vmodl types, not OSError, so they pass through
            # untouched to the handlers that already explain them.
            raise _connect_failed(target, exc) from exc
        def _cleanup(_si: ServiceInstance = si) -> None:
            # Sessions may already be dead at interpreter exit (timeout,
            # server restart) — never spray tracebacks during shutdown.
            try:
                Disconnect(_si)
            except Exception:
                pass

        atexit.register(_cleanup)
        return si


def get_content(si: ServiceInstance) -> vim.ServiceInstanceContent:
    """Shortcut to get ServiceContent from a ServiceInstance."""
    return si.RetrieveContent()
