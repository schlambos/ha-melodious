"""The Melodious hub's HTTP API, as this integration uses it."""

from __future__ import annotations

from typing import Any

import aiohttp

from .devices import hub_ha_devices


class HubError(Exception):
    """The hub couldn't be reached or didn't take the request."""


class HubRefused(HubError):
    """The hub's writer refused the device set (HTTP 422); retrying won't help."""


class HubClient:
    """codex_webui on the hub. Bodies are JSON with Content-Length; it ignores chunked ones."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._auth = aiohttp.BasicAuth(username, password or "") if username else None

    async def _request(
        self, method: str, path: str, payload: Any = None, timeout: int = 20, form: dict | None = None
    ) -> Any:
        try:
            async with self._session.request(
                method,
                self._base + path,
                json=payload if form is None else None,
                data=form,
                auth=self._auth,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 401:
                    raise HubError("the hub asked for a username and password")
                if response.status == 404:
                    raise HubError(f"the hub has no {path}; update Melodious on the hub")
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                if response.status == 422:
                    raise HubRefused((body or {}).get("error") or "the hub refused the change")
                if response.status != 200 or not isinstance(body, dict):
                    detail = (body or {}).get("error") if isinstance(body, dict) else None
                    raise HubError(detail or f"the hub answered HTTP {response.status}")
                return body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HubError(f"could not reach the hub: {err}") from err

    async def features(self) -> set[str]:
        """Feature flags from GET /api/version; empty when the hub predates it."""
        try:
            version = await self._request("GET", "/api/version")
        except HubError:
            return set()
        return set(version.get("features") or [])

    async def device_names(self) -> set[str]:
        """Names of the hub's devices that aren't Home Assistant devices."""
        config = await self._request("GET", "/api/activity-config")
        names = set()
        for entry in (config.get("deviceList") or {}).get("DevicesWithFeatures") or []:
            device = entry.get("Device") or {}
            tag = str((device.get("AutoDetectedDevice") or {}).get("Id") or "")
            if device.get("Name") and not tag.startswith("melodious-ha-"):
                names.add(str(device["Name"]))
        return names

    async def ha_devices(self) -> list[dict]:
        """The hub's current HA device specs, for seeding _previous on first run."""
        config = await self._request("GET", "/api/activity-config")
        return hub_ha_devices(config)

    async def activity_config(self) -> dict:
        return await self._request("GET", "/api/activity-config", timeout=30)

    async def activity_state(self) -> dict:
        return await self._request("GET", "/api/activity-state")

    async def run_activity(self, activity_id: str) -> dict:
        """Start an activity; "-1" is PowerOff. Form-encoded, like the hub's web app."""
        return await self._request("POST", "/api/activity-run", form={"activityId": activity_id}, timeout=60)

    async def send_command(
        self, device_id: str, command: str, status: str | None = None, hold_id: str | None = None
    ) -> dict:
        """POST /api/ir-send. It answers 200 even when the engine refuses; read `reply`."""
        form = {"deviceId": device_id, "command": command}
        if status:
            form.update(status=status, holdId=hold_id or "")
        return await self._request("POST", "/api/ir-send", form=form, timeout=30)

    async def commit(self, device_set: dict, dry_run: bool = False) -> dict:
        """Replace the hub's Home Assistant devices. The writer can take up to 45 s."""
        return await self._request("POST", "/api/ha-devices", {**device_set, "dryRun": dry_run}, timeout=120)
