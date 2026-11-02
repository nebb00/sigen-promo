"""Async client for the Sigen cloud promotional tariff API.

Behaviour confirmed against the live API (see tools/README.md in the repo):
- A save replaces ALL promos on the station, so every change is read-modify-write.
- A read returns every promo whose end date (or start date, if none) is on or after `dt`.
- The cloud silently drops promos dated in the past.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import aiohttp

from .const import base_url
from .promo import encrypt_password, normalise, sort_key

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)
DEVICE_ID = "sigenpromo" + "0" * 23
BASIC_AUTH = "Basic " + base64.b64encode(b"sigen:sigen").decode()


def app_headers(region: str) -> dict[str, str]:
    """Headers the web app sends for a region."""
    return {
        "lang": "en_US",
        "client-server": region,
        "AUTH-CLIENT-ID": "sigen",
        "VERSION": "RELEASE",
        "sg-app-version": "4.1.0",
        "sg-v": "4.1.0",
        "sg-platform": "web",
        "sg-source": "flutterweb",
        "sg-pkg": "sigen_app",
        "Origin": f"https://app-{region}.sigencloud.com",
        "Referer": f"https://app-{region}.sigencloud.com/",
    }


class SigenError(Exception):
    """The cloud refused a request or couldn't be reached."""


class SigenAuthError(SigenError):
    """Login failed; the user needs to re-enter their password."""


class SigenRefused(SigenError):
    """The cloud answered but rejected the request (e.g. invalid promo). Not worth retrying."""


# An expired or revoked access token comes back as HTTP 424 with this code, not 401.
TOKEN_EXPIRED_CODE = 2090


class SigenClient:
    """Logs in, keeps tokens fresh and reads/writes promos."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        region: str,
        username: str,
        password: str,
        tokens: dict[str, Any] | None = None,
        on_tokens: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        self._session = session
        self._base = base_url(region)
        self._headers = app_headers(region)
        self._username = username
        self._password = password
        self._tokens = dict(tokens or {})
        self._on_tokens = on_tokens
        self._auth_lock = asyncio.Lock()
        # Every change is read-modify-write of the whole list, so only one at a time.
        self.write_lock = asyncio.Lock()

    @property
    def tokens(self) -> dict[str, Any]:
        return dict(self._tokens)

    # ---- auth -------------------------------------------------------------

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{self._base}/auth/oauth/token",
                data={"scope": "server", **form},
                headers={"Authorization": BASIC_AUTH},
                timeout=TIMEOUT,
            ) as r:
                text = await r.text()
                status = r.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SigenError(f"Can't reach Sigen cloud: {err}") from err
        try:
            body = json.loads(text)
        except ValueError:
            body = {"msg": text[:200] or f"HTTP {status}"}
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        if status == 200 and data.get("access_token"):
            return data
        raise SigenAuthError(f"Login failed (HTTP {status}): {body.get('msg') or body}")

    async def _store(self, data: dict[str, Any]) -> None:
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or self._tokens.get("refresh_token"),
            "expires_at": time.time() + int(data.get("expires_in") or 43200),
        }
        if self._on_tokens:
            res = self._on_tokens(self.tokens)
            if asyncio.iscoroutine(res):
                await res

    async def login(self) -> None:
        data = await self._token_request(
            {
                "grant_type": "password",
                "userDeviceId": DEVICE_ID,
                "username": self._username,
                "password": encrypt_password(self._password),
            }
        )
        await self._store(data)

    async def _ensure_token(self, force: bool = False) -> str:
        async with self._auth_lock:
            fresh = self._tokens.get("expires_at", 0) - 600 > time.time()
            if self._tokens.get("access_token") and fresh and not force:
                return self._tokens["access_token"]
            if self._tokens.get("refresh_token"):
                try:
                    data = await self._token_request(
                        {"grant_type": "refresh_token", "refresh_token": self._tokens["refresh_token"]}
                    )
                    await self._store(data)
                    return self._tokens["access_token"]
                except SigenAuthError:
                    _LOGGER.debug("Refresh token rejected, logging in with password")
            await self.login()
            return self._tokens["access_token"]

    # ---- requests ---------------------------------------------------------

    async def _call(self, method: str, path: str, *, retry: bool = True, **kw) -> Any:
        token = await self._ensure_token()
        headers = {**self._headers, "Authorization": f"bearer {token}"}
        try:
            async with self._session.request(
                method, f"{self._base}{path}", headers=headers, timeout=TIMEOUT, **kw
            ) as r:
                status = r.status
                try:
                    body = await r.json(content_type=None)
                except ValueError:
                    body = None
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SigenError(f"Can't reach Sigen cloud: {err}") from err
        expired = status == 401 or (isinstance(body, dict) and body.get("code") == TOKEN_EXPIRED_CODE)
        if expired and retry:
            await self._ensure_token(force=True)
            return await self._call(method, path, retry=False, **kw)
        if expired:
            raise SigenAuthError("Sigen cloud rejected a freshly renewed login token")
        if not isinstance(body, dict):
            raise SigenError(f"Unexpected reply from Sigen cloud (HTTP {status})")
        if status >= 500:
            raise SigenError(f"Sigen cloud error (HTTP {status}): {body.get('msg') or body}")
        if status != 200 or body.get("code") != 0:
            raise SigenRefused(f"Sigen cloud refused it: {body.get('msg') or body}")
        return body.get("data")

    async def station_id(self) -> int:
        return int((await self.station_info())["stationId"])

    async def station_info(self, station: int | None = None) -> dict[str, Any]:
        """A station's details (stationId, stationShowName, timeZoneName, ...).

        Without an id, the account's home station.
        """
        path = f"/device/owner/station/{station}" if station else "/device/owner/station/home"
        return await self._call("GET", path)

    async def logout(self) -> None:
        """End this login session with the token we have, without logging in first.

        Errors are ignored: the tokens are being thrown away anyway.
        """
        token = self._tokens.get("access_token")
        if not token or self._tokens.get("expires_at", 0) < time.time():
            return
        try:
            async with self._session.delete(
                f"{self._base}/auth/token/logout",
                headers={**self._headers, "Authorization": f"bearer {token}"},
                timeout=TIMEOUT,
            ):
                pass
        except (TimeoutError, aiohttp.ClientError):
            pass

    async def get_promos(self, station: int, since: date) -> list[dict[str, Any]]:
        data = await self._call(
            "GET",
            "/electricity-price/api/promotional",
            params={"stationId": str(station), "dt": since.isoformat()},
        )
        return sorted((normalise(p) for p in (data or [])), key=sort_key)

    async def save_promos(self, station: int, promos: list[dict[str, Any]]) -> None:
        await self._call(
            "POST",
            "/electricity-price/api/promotional",
            json={"stationId": station, "promotionalList": sorted(promos, key=sort_key)},
        )
