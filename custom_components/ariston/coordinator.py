"""Coordinator class for Ariston module."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging

import aiohttp
from ariston.ariston_api import ConnectionException
from ariston.base_device import AristonBaseDevice
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class DeviceDataUpdateCoordinator(DataUpdateCoordinator):
    """Manages polling for state changes from the device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: AristonBaseDevice,
        scan_interval_seconds: int,
        coordinator_name: str,
        async_update_state: Callable,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize the data update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}-{device.name}-{coordinator_name}",
            update_interval=timedelta(seconds=scan_interval_seconds),
        )

        self.device = device
        self._async_update_state = async_update_state

    def provides(self, description) -> bool:
        """Return True if an entity description can be created for this device."""
        return True

    async def _async_update_data(self):
        """Fetch data, turning cloud/network failures into UpdateFailed.

        UpdateFailed is logged by Home Assistant as a single line (and only
        again once the coordinator recovers), instead of a full traceback on
        every failed poll.
        """
        try:
            return await self._async_update_state()
        except (ConnectionException, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Ariston cloud error: {err!r}") from err
