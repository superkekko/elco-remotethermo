"""The Ariston integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from ariston import Ariston, DeviceAttribute, SystemType
from ariston.ariston_api import ConnectionException
from ariston.const import ARISTON_API_URL, ARISTON_USER_AGENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_DEVICE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.util.unit_system import METRIC_SYSTEM

from .const import (
    API_URL_SETTING,
    API_USER_AGENT,
    BUS_ERRORS_COORDINATOR,
    BUS_ERRORS_SCAN_INTERVAL,
    COORDINATOR,
    DEFAULT_BUS_ERRORS_SCAN_INTERVAL_SECONDS,
    DEFAULT_ENERGY_SCAN_INTERVAL_MINUTES,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    ENERGY_COORDINATOR,
    EXTRA_COORDINATOR,
    ENERGY_SCAN_INTERVAL,
)
from .consumption import (
    DEFAULT_GAS_KWH_PER_SMC,
    GAS_KWH_PER_SMC,
    LEGACY_GALEVO_ONLY_NAMES,
    LEGACY_SENSOR_NAMES,
    EnergyDataUpdateCoordinator,
)
from .coordinator import DeviceDataUpdateCoordinator
from .extra_data import MENU_ITEM_IDS, ExtraDataUpdateCoordinator
from .menu_settings import LEGACY_MENU_NAMES, MENU_SETTINGS
from .resolver_fix import apply_system_resolver

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]

SERVICE_SET_ITEM_BY_ID = "set_item_by_id"
ATTR_ITEM_ID = "item_id"
ATTR_ZONE = "zone"
ATTR_VALUE = "value"

SET_ITEM_BY_ID_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_ITEM_ID): cv.string,
        vol.Required(ATTR_ZONE): vol.All(cv.positive_int, vol.Range(min=1)),
        vol.Required(ATTR_VALUE): vol.Coerce(float),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ariston from a config entry."""
    # Avoid sporadic aiodns/c-ares "Timeout while contacting DNS servers".
    # Remove this line to go back to the library's default resolver.
    apply_system_resolver()

    ariston = Ariston()
    try:
        api_url_setting = entry.data.get(API_URL_SETTING, ARISTON_API_URL)

        api_user_agent = entry.data.get(API_USER_AGENT, ARISTON_USER_AGENT)

        reponse = await ariston.async_connect(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            api_url_setting,
            api_user_agent,
        )
        if not reponse:
            _LOGGER.error(
                "Failed to connect to Ariston with device: %s",
                entry.data[CONF_DEVICE].get(DeviceAttribute.NAME),
            )
            raise ConfigEntryAuthFailed

        device = await ariston.async_hello(
            entry.data[CONF_DEVICE].get(DeviceAttribute.GW),
            hass.config.units is METRIC_SYSTEM,
        )
        if device is None:
            return False

        await device.async_get_features()

        scan_interval_seconds = entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
        )
        coordinator = DeviceDataUpdateCoordinator(
            hass,
            device,
            scan_interval_seconds,
            COORDINATOR,
            device.async_update_state,
            config_entry=entry,
        )

        hass.data.setdefault(DOMAIN, {}).setdefault(
            entry.unique_id,
            {COORDINATOR: {}, ENERGY_COORDINATOR: {}, EXTRA_COORDINATOR: {}},
        )
        hass.data[DOMAIN][entry.unique_id][COORDINATOR] = coordinator

        await coordinator.async_config_entry_first_refresh()

        bus_errors_scan_interval_seconds = entry.options.get(
            BUS_ERRORS_SCAN_INTERVAL, DEFAULT_BUS_ERRORS_SCAN_INTERVAL_SECONDS
        )
        bus_errors_coordinator = DeviceDataUpdateCoordinator(
            hass,
            device,
            bus_errors_scan_interval_seconds,
            BUS_ERRORS_COORDINATOR,
            device.async_get_bus_errors,
            config_entry=entry,
        )
        hass.data[DOMAIN][entry.unique_id][BUS_ERRORS_COORDINATOR] = (
            bus_errors_coordinator
        )
        await bus_errors_coordinator.async_refresh()

        if device.has_metering:
            energy_interval_minutes = entry.options.get(
                ENERGY_SCAN_INTERVAL, DEFAULT_ENERGY_SCAN_INTERVAL_MINUTES
            )
            energy_coordinator = EnergyDataUpdateCoordinator(
                hass,
                device,
                energy_interval_minutes * 60,
                ENERGY_COORDINATOR,
                entry.options.get(GAS_KWH_PER_SMC, DEFAULT_GAS_KWH_PER_SMC),
                config_entry=entry,
            )
            hass.data[DOMAIN][entry.unique_id][ENERGY_COORDINATOR] = energy_coordinator
            # Energy data is optional: do not block setup if it fails.
            await energy_coordinator.async_load_lifetime()
            await energy_coordinator.async_refresh()

        if device.system_type == SystemType.GALEVO:
            # Extra menu items / plantData. Optional: never blocks the setup.
            extra_coordinator = ExtraDataUpdateCoordinator(
                hass,
                device,
                scan_interval_seconds,
                EXTRA_COORDINATOR,
                api_url_setting,
                MENU_ITEM_IDS,
                [setting.portal_id for setting in MENU_SETTINGS],
                config_entry=entry,
            )
            hass.data[DOMAIN][entry.unique_id][EXTRA_COORDINATOR] = extra_coordinator
            await extra_coordinator.async_refresh()

        _remove_legacy_consumption_sensors(
            hass, entry, device.gateway, device.system_type == SystemType.GALEVO
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        entry.async_on_unload(entry.add_update_listener(update_listener))

        if device.system_type == SystemType.GALEVO:

            async def async_set_item_by_id_service(service_call):
                """Create a vacation on the target device."""
                device_id = service_call.data.get(ATTR_DEVICE_ID)
                item_id = service_call.data.get(ATTR_ITEM_ID)
                zone = service_call.data.get(ATTR_ZONE)
                value = service_call.data.get(ATTR_VALUE)

                device_registry = dr.async_get(hass)
                device = device_registry.devices[device_id]

                entry = hass.config_entries.async_get_entry(
                    next(iter(device.config_entries))
                )
                coordinator: DeviceDataUpdateCoordinator = hass.data[DOMAIN][
                    entry.unique_id
                ][COORDINATOR]
                await coordinator.device.async_set_item_by_id(item_id, value, zone)

            hass.services.async_register(
                DOMAIN,
                SERVICE_SET_ITEM_BY_ID,
                async_set_item_by_id_service,
                schema=SET_ITEM_BY_ID_SCHEMA,
            )
    except (ConfigEntryAuthFailed, ConfigEntryNotReady):
        raise
    except ConnectionException as error:
        # Cloud/network problem: Home Assistant retries with backoff, no traceback.
        raise ConfigEntryNotReady(
            f"Ariston cloud not reachable: {error!r}"
        ) from error
    except Exception as error:
        _LOGGER.exception("Unexpected error setting up Ariston")
        raise ConfigEntryNotReady from error

    return True


def _remove_legacy_consumption_sensors(
    hass: HomeAssistant, entry: ConfigEntry, gateway: str, galevo: bool = True
) -> None:
    """Remove the old (wrong) consumption sensors, replaced by consumption.py."""
    registry = er.async_get(hass)
    names = [*LEGACY_SENSOR_NAMES, *(LEGACY_GALEVO_ONLY_NAMES if galevo else [])]
    legacy_ids = {f"{gateway}-{name}" for name in names}
    legacy_menu_ids = {f"{gateway}-{name}" for name in LEGACY_MENU_NAMES}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain == Platform.SENSOR and entity.unique_id in legacy_ids) or (
            entity.domain
            in (Platform.NUMBER, Platform.SWITCH, Platform.SELECT)
            and entity.unique_id in legacy_menu_ids
        ):
            _LOGGER.info("Removing legacy consumption sensor %s", entity.entity_id)
            registry.async_remove(entity.entity_id)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.unique_id)

    return unload_ok
