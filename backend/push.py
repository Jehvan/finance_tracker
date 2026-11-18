"""Web push: subscription validation, VAPID keys and sending.

The server POSTs to whatever endpoint URL a browser subscribed with, so
endpoints are restricted to the real browser push services. Without that, a
logged-in client could make this server send requests to arbitrary hosts (SSRF).
"""
import base64
import json
import re
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

# Chrome/Edge/Android (FCM), Firefox, Windows (WNS) and Safari/iOS.
ALLOWED_PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)
MAX_ENDPOINT_LEN = 1000
B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
NOTIFICATION_TTL_SECONDS = 12 * 3600  # a reminder older than half a day isn't worth delivering
SEND_TIMEOUT_SECONDS = 10


def _host_allowed(host):
    return any(host == s or host.endswith("." + s) for s in ALLOWED_PUSH_HOST_SUFFIXES)


def _b64url_bytes(value, field, min_len, max_len):
    if not isinstance(value, str) or not B64URL_RE.match(value) or len(value) > 200:
        raise ValueError(f"{field} must be base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        raise ValueError(f"{field} must be base64url")
    if not min_len <= len(raw) <= max_len:
        raise ValueError(f"{field} has the wrong length")
    return value


def clean_subscription(data):
    """Validate a browser PushSubscription (its toJSON() form). Returns
    {endpoint, p256dh, auth} or raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("subscription must be a JSON object")
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_LEN:
        raise ValueError("endpoint is missing or too long")
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("endpoint must be a plain https URL")
    if not parts.hostname or not _host_allowed(parts.hostname.lower()):
        raise ValueError("endpoint is not a known browser push service")
    keys = data.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("keys are missing")
    return {
        "endpoint": endpoint,
        # P-256 public key (65 bytes uncompressed) and a 16-byte auth secret.
        "p256dh": _b64url_bytes(keys.get("p256dh"), "p256dh", 65, 65),
        "auth": _b64url_bytes(keys.get("auth"), "auth", 16, 16),
    }


def load_vapid_key(private_key_b64):
    """The server's VAPID key from VAPID_PRIVATE_KEY (base64url raw 32-byte
    P-256 private key), or None when push isn't configured.

    Raises ValueError for a malformed value (e.g. the public key pasted by mistake)."""
    if not private_key_b64 or not private_key_b64.strip():
        return None
    value = private_key_b64.strip()
    if not B64URL_RE.match(value):
        raise ValueError("VAPID_PRIVATE_KEY must be base64url")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != 32:
        raise ValueError(f"VAPID_PRIVATE_KEY must decode to 32 bytes, got {len(raw)}")
    try:
        return Vapid02.from_string(value)
    except Exception as e:  # not a valid P-256 private key
        raise ValueError(f"VAPID_PRIVATE_KEY is not a valid P-256 key: {type(e).__name__}")


def public_key_b64(vapid):
    """The applicationServerKey browsers subscribe with (uncompressed point, base64url)."""
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_private_key_b64():
    """A new VAPID private key in the format VAPID_PRIVATE_KEY expects."""
    v = Vapid02()
    v.generate_keys()
    raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _no_redirect_session():
    # Push services answer directly; never follow a redirect to another host.
    session = requests.Session()
    session.max_redirects = 0
    return session


def send_notification(subscription, payload, vapid, subject):
    """Send one notification. Returns "sent", "gone" (the browser unsubscribed;
    delete it) or "failed" (try again another day)."""
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=vapid,
            vapid_claims={"sub": subject},
            ttl=NOTIFICATION_TTL_SECONDS,
            timeout=SEND_TIMEOUT_SECONDS,
            requests_session=_no_redirect_session(),
        )
        return "sent"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        return "gone" if status in (404, 410) else "failed"
    except Exception:  # network errors, timeouts: never let one device break the run
        return "failed"


if __name__ == "__main__":
    # python push.py  -> prints a fresh VAPID_PRIVATE_KEY value to put in Render's env vars
    print(generate_private_key_b64())
