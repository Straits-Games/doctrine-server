"""Discord OAuth2 (authorization-code grant, `identify` scope) over httpx.

Pass an `httpx` transport (e.g. `httpx.MockTransport`) to test without the network.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
API_BASE = "https://discord.com/api/v10"


class OAuthError(Exception):
    pass


class DiscordOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:   # never show the secret
        return f"DiscordOAuth(client_id={self.client_id!r}, redirect_uri={self.redirect_uri!r})"

    def authorize_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "identify",
            "redirect_uri": self.redirect_uri,
            "state": state,
        })
        return f"{AUTHORIZE_URL}?{query}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout,
                                 headers={"User-Agent": "doctrine-server"})

    async def exchange_code(self, code: str) -> str:
        """Trade the authorization code for an access token."""
        try:
            async with self._client() as client:
                r = await client.post(TOKEN_URL, data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                }, auth=(self.client_id, self._client_secret))
        except httpx.HTTPError as e:
            raise OAuthError(f"token request failed: {type(e).__name__}") from None
        if r.status_code != 200:
            raise OAuthError(f"token request returned HTTP {r.status_code}")
        try:
            token = r.json().get("access_token")
        except ValueError:
            token = None
        if not token:
            raise OAuthError("token response had no access_token")
        return token

    async def fetch_user(self, access_token: str) -> dict:
        """GET /users/@me -> {id, username, global_name, ...}."""
        try:
            async with self._client() as client:
                r = await client.get(f"{API_BASE}/users/@me",
                                     headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as e:
            raise OAuthError(f"user request failed: {type(e).__name__}") from None
        if r.status_code != 200:
            raise OAuthError(f"user request returned HTTP {r.status_code}")
        try:
            user = r.json()
        except ValueError:
            raise OAuthError("user response was not JSON") from None
        if not isinstance(user, dict) or "id" not in user:
            raise OAuthError("user response had no id")
        return user
