"""One shared poll of the hub's catalogue and running activity."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .catalogue import Catalogue, current_activity_id
from .const import DOMAIN
from .hub import HubClient, HubError

_LOGGER = logging.getLogger(__name__)

POLL = timedelta(seconds=30)
# One failed poll keeps the last answer, so a blip doesn't flap the entities.
GRACE_FAILURES = 1


class MelodiousCoordinator(DataUpdateCoordinator[tuple[Catalogue, str | None]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HubClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=POLL,
        )
        self.client = client
        self._failures = 0

    @property
    def catalogue(self) -> Catalogue:
        return self.data[0] if self.data else Catalogue((), ())

    @property
    def activity_id(self) -> str | None:
        return self.data[1] if self.data else None

    async def _async_update_data(self) -> tuple[Catalogue, str | None]:
        try:
            config = await self.client.activity_config()
            state = await self.client.activity_state()
        except HubError as err:
            self._failures += 1
            if self.data is not None and self._failures <= GRACE_FAILURES:
                return self.data
            raise UpdateFailed(str(err)) from err
        self._failures = 0
        return Catalogue.from_config(config), current_activity_id(state)
