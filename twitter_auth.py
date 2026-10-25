"""Sign in with X (Twitter): OAuth 2.0 Authorization Code flow with PKCE, confidential client.

  TWITTER_CLIENT_ID / TWITTER_CLIENT_SECRET   from developer.x.com (OAuth 2.0 keys of your app; type: Web App / confidential client)
  TWITTER_REDIRECT_URI                        must match the callback URL registered in the X app EXACTLY, e.g.
                                              https://your-site/auth/twitter/callback (defaults to <site address>/auth/twitter/callback)

Only the minimum is requested: scopes tweet.read + users.read (needed for GET /2/users/me). No refresh token is stored.
"""
import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
ME_URL = "https://api.twitter.com/2/users/me?user.fields=profile_image_url"
SCOPES = "tweet.read users.read"
TIMEOUT_SECONDS = 15


class TwitterError(Exception):
    pass


def config() -> dict:
    return {
        "client_id": os.getenv("TWITTER_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("TWITTER_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv("TWITTER_REDIRECT_URI", "").strip(),
    }


def is_configured() -> bool:
    cfg = config()
    return bool(cfg["client_id"] and cfg["client_secret"])


def new_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config()["client_id"],
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{query}"


def _request(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        print(f"[x-login] {request.full_url} -> HTTP {e.code} {e.read()[:300]!r}")
        raise TwitterError(f"HTTP {e.code}")
    except Exception as e:
        print(f"[x-login] {request.full_url} failed: {e!r}")
        raise TwitterError("request failed")


def exchange_code(code: str, verifier: str, redirect_uri: str) -> str:
    """Trades the authorization code for an access token (client authenticates with HTTP Basic)."""
    cfg = config()
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    body = urllib.parse.urlencode({
        "code": code,
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode()
    data = _request(urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "accounting-trainer/1.0",
    }))
    token = data.get("access_token")
    if not token:
        raise TwitterError("no access token in the response")
    return token


def fetch_me(access_token: str) -> dict:
    """{'id', 'username', 'name', 'profile_image_url'} of the signed-in X account."""
    data = _request(urllib.request.Request(ME_URL, headers={
        "Authorization": "Bearer " + access_token,
        "User-Agent": "accounting-trainer/1.0",
    }))
    me = data.get("data") or {}
    if not me.get("id"):
        raise TwitterError("no user in the response")
    return me
