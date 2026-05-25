"""
Epic Games OAuth handler.

Uses the well-known public Epic Games Launcher client ID, the same one tools
like Legendary (https://github.com/derrod/legendary) and Heroic Games Launcher
have used reliably for years.  Refresh tokens are stored encrypted on disk via
the Windows Data Protection API (DPAPI), which ties them to the current user
account — another user on the same machine cannot decrypt them.

The OAuth flow we implement here is the "authorization code with paste-back"
variant used by every desktop Epic third-party tool, because Epic doesn't
expose a way to register a custom redirect URI for a desktop app:

    1. User opens https://www.epicgames.com/id/login?... in their browser
    2. After logging in, Epic redirects to /id/api/redirect?... which returns
       a JSON page containing {"authorizationCode": "..."}
    3. User copies the code, pastes it into our app
    4. We POST it to the token endpoint, exchange it for access + refresh tokens
    5. Refresh token is stored encrypted; access tokens are refreshed silently
       as needed on subsequent launches

No external dependencies — only stdlib.  HTTP via urllib, encryption via
ctypes-wrapped DPAPI.
"""

import base64
import ctypes
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes


# ── Public Epic Launcher credentials ─────────────────────────────────────────
#
# These are the credentials baked into Epic's own desktop launcher; they're
# common knowledge and used by every Epic third-party tool.  Epic has never
# rotated them in the >4 years tools like Legendary have relied on them.

EPIC_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
EPIC_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
EPIC_USER_AGENT    = ("EpicGamesLauncher/14.0.8-22004686+++Portal+Release-Live "
                      "Windows/10.0.19044.1.768.64bit")

# OAuth endpoints
LOGIN_URL    = "https://www.epicgames.com/id/login"
REDIRECT_URL = "https://www.epicgames.com/id/api/redirect"
TOKEN_URL    = ("https://account-public-service-prod.ol.epicgames.com"
                "/account/api/oauth/token")


# ─────────────────────────────────────────────────────────────────────────────
#  Login URL
# ─────────────────────────────────────────────────────────────────────────────

def get_login_url():
    """Return the URL the user opens in their browser to start the OAuth flow.

    After they log in, Epic redirects them to a page whose body contains a JSON
    object with the authorization code they need to paste back into our app.
    """
    redirect = f"{REDIRECT_URL}?clientId={EPIC_CLIENT_ID}&responseType=code"
    return f"{LOGIN_URL}?redirectUrl={urllib.parse.quote(redirect, safe='')}"


# ─────────────────────────────────────────────────────────────────────────────
#  Windows DPAPI — encrypted-at-rest storage for the refresh token
# ─────────────────────────────────────────────────────────────────────────────
#
# DPAPI ties encryption to the current Windows user account, so the encrypted
# blob can only be decrypted by us, running as the same user, on the same
# machine.  This is the standard Windows mechanism for storing secrets at rest
# without prompting the user for a passphrase.

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _setup_dpapi():
    """Configure the ctypes prototypes for CryptProtectData / CryptUnprotectData
    once.  Called lazily so import doesn't fail on non-Windows (we never run
    there in practice but keep the module importable for tests)."""
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),  # pDataIn
        wintypes.LPCWSTR,            # szDataDescr
        ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
        ctypes.c_void_p,             # pvReserved
        ctypes.c_void_p,             # pPromptStruct
        wintypes.DWORD,              # dwFlags
        ctypes.POINTER(_DATA_BLOB),  # pDataOut
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = crypt32.CryptProtectData.argtypes
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32


def _bytes_to_blob(data):
    """Wrap a Python bytes object as a DATA_BLOB suitable for the WinAPI."""
    buf = (ctypes.c_byte * len(data))(*data)
    return _DATA_BLOB(len(data), buf)


def _dpapi_encrypt(plaintext):
    crypt32 = _setup_dpapi()
    blob_in = _bytes_to_blob(plaintext)
    blob_out = _DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out),
    ):
        raise OSError(f"CryptProtectData failed (err={ctypes.GetLastError()})")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _dpapi_decrypt(ciphertext):
    crypt32 = _setup_dpapi()
    blob_in = _bytes_to_blob(ciphertext)
    blob_out = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out),
    ):
        raise OSError(f"CryptUnprotectData failed (err={ctypes.GetLastError()})")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Token storage on disk
# ─────────────────────────────────────────────────────────────────────────────

def _token_file(cache_dir):
    return os.path.join(cache_dir, "epic_tokens.bin")


def store_tokens(cache_dir, tokens):
    """Encrypt and write the token dict to disk."""
    data = json.dumps(tokens).encode("utf-8")
    blob = _dpapi_encrypt(data)
    path = _token_file(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)


def load_tokens(cache_dir):
    """Decrypt and return the stored token dict, or None if missing/corrupt."""
    path = _token_file(cache_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            blob = f.read()
        return json.loads(_dpapi_decrypt(blob).decode("utf-8"))
    except Exception:
        return None


def clear_tokens(cache_dir):
    """Remove stored tokens (effectively, log out)."""
    path = _token_file(cache_dir)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  OAuth HTTP calls
# ─────────────────────────────────────────────────────────────────────────────

def _basic_auth_header():
    creds = f"{EPIC_CLIENT_ID}:{EPIC_CLIENT_SECRET}".encode("ascii")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


def _post_form(url, form_data):
    """POST form-encoded body to Epic and parse the JSON response."""
    body = urllib.parse.urlencode(form_data).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", EPIC_USER_AGENT)
    req.add_header("Authorization", _basic_auth_header())
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_auth_code(code):
    """Trade an authorization code (one-time, just-issued) for an access +
    refresh token pair.  Returns the full token dict as Epic returned it."""
    return _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "token_type": "eg1",
    })


def refresh_access_token(refresh_token):
    """Use a refresh token to get a fresh access token (and a new refresh token
    — Epic rotates refresh tokens on each refresh)."""
    return _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "token_type": "eg1",
    })


# ─────────────────────────────────────────────────────────────────────────────
#  High-level helpers used by the rest of the app
# ─────────────────────────────────────────────────────────────────────────────

def _tag_expiry(tokens):
    """Add an absolute 'expires_at' timestamp to a fresh token dict so we can
    cheaply check whether the access token is still valid later.  We subtract
    30 seconds as a buffer for clock skew + network latency."""
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600) - 30
    return tokens


def complete_auth(cache_dir, code):
    """Run the auth-code-to-tokens exchange and persist the result.  Returns a
    small status dict with the user's displayName + account_id.  Raises on
    HTTP failure so the caller can surface a clean error to the user."""
    tokens = _tag_expiry(exchange_auth_code(code.strip()))
    store_tokens(cache_dir, tokens)
    return {
        "displayName": tokens.get("displayName"),
        "account_id":  tokens.get("account_id"),
    }


def get_valid_token(cache_dir):
    """Return (tokens, status).

    status is one of:
      * 'ok'                  — tokens valid, access token usable
      * 'not_authenticated'   — no tokens stored
      * 'refresh_expired'     — refresh token rejected; user must re-auth
      * 'error: <message>'    — transient HTTP/network failure
    """
    tokens = load_tokens(cache_dir)
    if not tokens:
        return None, "not_authenticated"

    # Access token still valid? (with a small safety margin)
    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens, "ok"

    # Otherwise, refresh
    refresh = tokens.get("refresh_token")
    if not refresh:
        return None, "no_refresh_token"

    try:
        new_tokens = _tag_expiry(refresh_access_token(refresh))
        store_tokens(cache_dir, new_tokens)
        return new_tokens, "ok"
    except urllib.error.HTTPError as e:
        # 400/401 typically means the refresh token has expired or been revoked.
        # Wipe local state so the user gets a clean re-auth prompt next time.
        if e.code in (400, 401):
            clear_tokens(cache_dir)
            return None, "refresh_expired"
        return None, f"error: HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"error: {e}"


def get_status(cache_dir):
    """Return a connection-status summary suitable for the Settings UI:
        { connected: bool, displayName: str|None, account_id: str|None,
          error: str|None }
    Does NOT trigger a refresh — purely inspects what's on disk."""
    tokens = load_tokens(cache_dir)
    if not tokens:
        return {"connected": False, "displayName": None, "account_id": None}
    return {
        "connected":   True,
        "displayName": tokens.get("displayName"),
        "account_id":  tokens.get("account_id"),
    }
