"""Minimal endpoint identity, distinct from host inventory and release verdicts."""
from vmware_policy import sanitize


def get_endpoint_identity(si) -> dict:
    """Read ServiceContent metadata; absent/non-string facts remain unknown."""
    result = {"api_type": None, "version": None, "build": None}
    try:
        about = getattr(getattr(si, "content", None), "about", None)
        fields = (("api_type", "apiType"), ("version", "version"), ("build", "build"))
        for name, attribute in fields:
            value = getattr(about, attribute, None)
            if isinstance(value, str) and value.strip():
                result[name] = sanitize(value.strip())
    except Exception as exc:
        # Optional identity must not discard already collected health evidence.
        # Do not expose an exception message containing credentials or addresses.
        result = {"api_type": None, "version": None, "build": None,
                  "unavailable": f"Endpoint metadata unavailable ({type(exc).__name__})."}
    return result
