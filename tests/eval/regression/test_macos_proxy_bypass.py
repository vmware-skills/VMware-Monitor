"""A vCenter REST request honors the system's target-specific proxy bypass."""

import urllib.request

import httpx

from vmware_monitor.config import TargetConfig
from vmware_monitor.rest import VsphereRest


def test_rest_client_routes_only_bypassed_origin_directly(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == "target.invalid")
    options = []
    direct = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))

    def transport(**kwargs):
        options.append(kwargs)
        return direct

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    target = TargetConfig(
        name="test", host="target.invalid", config_username="admin", verify_ssl=False
    )
    with VsphereRest(target)._client() as client:
        assert client.get("/api/vcenter").json() == {"status": "ok"}
        assert client._transport_for_url(httpx.URL("https://other.invalid/")) is not direct
    assert options == [{"verify": False, "trust_env": True}]
