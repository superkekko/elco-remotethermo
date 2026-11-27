"""Extra read-only data from the Ariston cloud (Galevo systems).

The cloud exposes many more values than the library requests by default:

* "menu items": numeric ids, the same values shown in the boiler menu
  (GET menuItems/<gw>?menuItems=<id>,<id>,...)
* "plantData": a flat summary of the plant (GET remote/plantData/<gw>)

Everything here is READ ONLY. One coordinator fetches both in a single poll and
the entities below read from it. An entity is only created if the cloud
actually returns the item for this device.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ariston.const import ARISTON_MENU_ITEMS, ARISTON_REMOTE, PlantData, SystemType
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import (
    EXTRA_COORDINATOR,
    EXTRA_STATE_ATTRIBUTE,
    EXTRA_STATE_DEVICE_METHOD,
    NAME,
    AristonBaseEntityDescription,
    AristonBinarySensorEntityDescription,
    AristonSensorEntityDescription,
)
from .coordinator import DeviceDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Menu item ids (from a real Ariston NET hybrid system).
GAS_POWER = 125
CH_FLOW_TEMP_BOILER = 123
CH_FLOW_TEMP_HYBRID = 349
HP_WATER_FLOW_TEMP = 348
HEAT_PUMP_MODE = 340
DIVERTER_VALVE_POS = 129
PUMP_MODULATION = 115
ZONE1_HUMIDITY = 146
BUFFER_CONTROL_MODE = 368
BUFFER_COMFORT_SETPOINT = 369
BUFFER_REDUCED_SETPOINT = 371
BUFFER_ACTIVATION = 409
ENERGY_MANAGER_LOGIC = 455
TANK_CHARGE_MODE = 480
GAS_COST = 446
ELECTRICITY_COST = 449
ELECTRICITY_COST_LOW = 452

PLANT_FLAME_FOR_DHW = "flameForDhw"

# The cloud reports "no sensor" as the maximum (3276) / minimum (-3277) value.
_SENTINEL = 3276


class ExtraDataUpdateCoordinator(DeviceDataUpdateCoordinator):
    """Polls menu items and plantData; both parts are optional and independent."""

    def __init__(
        self,
        hass: HomeAssistant,
        device,
        scan_interval_seconds: int,
        coordinator_name: str,
        api_url: str,
        menu_item_ids: list[int],
        portal_ids: list[str] | None = None,
        config_entry=None,
    ) -> None:
        """Initialize the coordinator."""
        self._api_url = api_url if api_url.endswith("/") else f"{api_url}/"
        self._menu_item_ids = sorted(set(menu_item_ids))
        self._portal_ids = sorted(set(portal_ids or []))
        super().__init__(
            hass,
            device,
            scan_interval_seconds,
            coordinator_name,
            self._async_fetch,
            config_entry=config_entry,
        )

    async def _async_fetch(self) -> dict[str, Any]:
        """Fetch menu items and plant data.

        If only one of the two requests fails, the previous value of that part
        is kept; if both fail the update fails (entities become unavailable).
        """
        gw = self.device.gateway
        api = self.device.api
        ids = ",".join(str(i) for i in self._menu_item_ids)
        results = await asyncio.gather(
            api._async_get(f"{self._api_url}{ARISTON_MENU_ITEMS}/{gw}?menuItems={ids}"),
            api._async_get(f"{self._api_url}{ARISTON_REMOTE}/{PlantData.PD.value}/{gw}"),
            self._async_fetch_portal(api, gw),
            return_exceptions=True,
        )
        portal_res = results[2]
        results = results[:2]
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result  # e.g. CancelledError
        errors = [r for r in results if isinstance(r, Exception)]
        if len(errors) == len(results):
            raise errors[0]

        previous = self.data or {"menu": None, "plant": None, "portal": {}}
        menu_res, plant_res = results

        if isinstance(menu_res, Exception):
            _LOGGER.debug("Menu items not updated: %r", menu_res)
            menu = previous["menu"]
        else:
            menu = {
                item["id"]: item
                for item in (menu_res or [])
                if isinstance(item, dict) and "id" in item
            }

        if isinstance(plant_res, Exception):
            _LOGGER.debug("Plant data not updated: %r", plant_res)
            plant = previous["plant"]
        else:
            plant = plant_res if isinstance(plant_res, dict) else {}

        if isinstance(portal_res, Exception) or portal_res is None:
            portal = previous.get("portal") or {}
        else:
            portal = portal_res
        return {"menu": menu, "plant": plant, "portal": portal}

    async def _async_fetch_portal(self, api, gw) -> dict[str, Any] | None:
        """Try to read menu items by portal id (e.g. U6_15_2). Optional.

        Not every cloud answers this; any failure is ignored and the entities
        fall back to the last written / restored value.
        """
        if not self._portal_ids:
            return None
        ids = ",".join(self._portal_ids)
        items = await api._async_get(
            f"{self._api_url}{ARISTON_MENU_ITEMS}/{gw}?menuItems={ids}"
        )
        result: dict[str, Any] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            keys = {str(item.get("id"))}
            node = item.get("nodeId")
            if isinstance(node, str):
                keys.add(re.sub(r"^([A-Z])_", r"\1", node).replace(".", "_"))
            for key in keys & set(self._portal_ids):
                result[key] = item
        _LOGGER.debug("Portal menu items read: %s", sorted(result))
        return result

    def portal_value(self, portal_id: str) -> float | None:
        """Value of a menu item read by portal id, if the cloud returned it."""
        item = ((self.data or {}).get("portal") or {}).get(portal_id)
        value = item.get("value") if item else None
        return value if isinstance(value, (int, float)) else None

    # -- accessors used by the entity descriptions ---------------------------

    def menu_item(self, item_id: int) -> dict[str, Any] | None:
        """Return the raw menu item, if known."""
        menu = (self.data or {}).get("menu")
        return menu.get(item_id) if menu else None

    def menu_value(self, item_id: int) -> float | None:
        """Return the numeric value, or None if missing / "no sensor"."""
        item = self.menu_item(item_id)
        if not item:
            return None
        value = item.get("value")
        if value is None:
            return None
        maximum, minimum = item.get("max"), item.get("min")
        if isinstance(maximum, (int, float)) and maximum >= _SENTINEL and value >= maximum:
            return None
        if isinstance(minimum, (int, float)) and minimum <= -_SENTINEL and value <= minimum:
            return None
        return value

    def menu_text(self, item_id: int) -> str | None:
        """Return the text of an enumerated item (e.g. 'Standby')."""
        value = self.menu_value(item_id)
        if value is None:
            return None
        options = (self.menu_item(item_id) or {}).get("options") or {}
        return options.get(str(int(value)), f"{value:g}")

    def menu_unit(self, item_id: int) -> str | None:
        """Return the unit, or None when the item has none."""
        return (self.menu_item(item_id) or {}).get("unit") or None

    def plant_value(self, key: str) -> Any:
        """Return a value from plantData."""
        return ((self.data or {}).get("plant") or {}).get(key)

    def provides(self, description: AristonBaseEntityDescription) -> bool:
        """Create an entity only if the cloud returns its data (or we don't know yet)."""
        data = self.data or {}
        if description.required_menu_item is not None:
            menu = data.get("menu")
            if menu is not None and description.required_menu_item not in menu:
                return False
        if description.required_plant_key is not None:
            plant = data.get("plant")
            if plant is not None and description.required_plant_key not in plant:
                return False
        return True


def _number(
    item_id: int,
    name: str,
    *,
    device_class: SensorDeviceClass | None = None,
    icon: str | None = None,
    category: EntityCategory | None = None,
    measurement: bool = True,
) -> AristonSensorEntityDescription:
    """Read-only numeric sensor from a menu item."""
    return AristonSensorEntityDescription(
        key=f"menu_item_{item_id}",
        name=f"{NAME} {name}",
        device_class=device_class,
        icon=icon,
        entity_category=category,
        state_class=SensorStateClass.MEASUREMENT if measurement else None,
        coordinator=EXTRA_COORDINATOR,
        required_menu_item=item_id,
        get_native_value=lambda entity, i=item_id: entity.coordinator.menu_value(i),
        get_native_unit_of_measurement=lambda entity, i=item_id: entity.coordinator.menu_unit(i),
        system_types=[SystemType.GALEVO],
    )


def _text(
    item_id: int,
    name: str,
    *,
    icon: str | None = None,
    category: EntityCategory | None = None,
) -> AristonSensorEntityDescription:
    """Read-only text sensor from an enumerated menu item (raw value as attribute)."""
    return AristonSensorEntityDescription(
        key=f"menu_item_{item_id}",
        name=f"{NAME} {name}",
        icon=icon,
        entity_category=category,
        coordinator=EXTRA_COORDINATOR,
        required_menu_item=item_id,
        get_native_value=lambda entity, i=item_id: entity.coordinator.menu_text(i),
        extra_states=[
            {
                EXTRA_STATE_ATTRIBUTE: "value",
                EXTRA_STATE_DEVICE_METHOD: lambda entity, i=item_id: entity.coordinator.menu_value(i),
            }
        ],
        system_types=[SystemType.GALEVO],
    )


_DIAG = EntityCategory.DIAGNOSTIC
_TEMP = SensorDeviceClass.TEMPERATURE

# Gas power, boiler/HP flow temperature, HP mode and pump modulation are now
# provided by the base integration (const.py), so they are not repeated here.
ARISTON_EXTRA_SENSOR_TYPES: list[AristonSensorEntityDescription] = [
    _number(CH_FLOW_TEMP_HYBRID, "CH flow temp (hybrid)", device_class=_TEMP),
    _text(DIVERTER_VALVE_POS, "diverter valve position", icon="mdi:valve"),
    _number(ZONE1_HUMIDITY, "zone 1 humidity", device_class=SensorDeviceClass.HUMIDITY),
    _text(BUFFER_ACTIVATION, "buffer activation", icon="mdi:database-check", category=_DIAG),
    _text(BUFFER_CONTROL_MODE, "buffer control mode", icon="mdi:database-clock", category=_DIAG),
    _number(BUFFER_COMFORT_SETPOINT, "buffer comfort setpoint", device_class=_TEMP, category=_DIAG),
    _number(BUFFER_REDUCED_SETPOINT, "buffer reduced setpoint", device_class=_TEMP, category=_DIAG),
    _text(ENERGY_MANAGER_LOGIC, "energy manager logic", icon="mdi:leaf", category=_DIAG),
    _text(TANK_CHARGE_MODE, "tank charge mode", icon="mdi:water-boiler", category=_DIAG),
    _number(GAS_COST, "gas cost", icon="mdi:cash", category=_DIAG, measurement=False),
    _number(ELECTRICITY_COST, "electricity cost", icon="mdi:cash", category=_DIAG, measurement=False),
    _number(
        ELECTRICITY_COST_LOW,
        "electricity cost (low tariff)",
        icon="mdi:cash",
        category=_DIAG,
        measurement=False,
    ),
]

ARISTON_EXTRA_BINARY_SENSOR_TYPES: list[AristonBinarySensorEntityDescription] = [
    AristonBinarySensorEntityDescription(
        key=PLANT_FLAME_FOR_DHW,
        name=f"{NAME} flame for DHW",
        icon="mdi:water-boiler",
        coordinator=EXTRA_COORDINATOR,
        required_plant_key=PLANT_FLAME_FOR_DHW,
        get_is_on=lambda entity: entity.coordinator.plant_value(PLANT_FLAME_FOR_DHW),
        system_types=[SystemType.GALEVO],
    ),
]

MENU_ITEM_IDS: list[int] = sorted(
    {d.required_menu_item for d in ARISTON_EXTRA_SENSOR_TYPES if d.required_menu_item}
)
