"""Writable settings from the Ariston NET *menu* (the "6.x" menu of the website).

These items are not part of the library's dataItems, but the cloud accepts them
through the same ``dataItems/set`` call using the portal id of the item
(``U_6.15.2`` -> ``U6_15_2``). This was verified on a real system for 6.15.2.

Reading: the cloud does not return most of these items (no numeric menu id).
Entities with ``read_menu_id`` follow the value polled by the extra coordinator;
the others are optimistic: they show the last value written from Home Assistant
(restored after a restart). Only user-level settings are exposed, never
installer parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from ariston import SystemType
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    COORDINATOR,
    DOMAIN,
    EXTRA_COORDINATOR,
    NAME,
    AristonBaseEntityDescription,
)
from .coordinator import DeviceDataUpdateCoordinator
from .entity import AristonEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MenuSetting:
    """One writable menu item."""

    key: str
    name: str
    portal_id: str
    kind: str  # "number" | "select" | "switch"
    icon: str | None = None
    unit: str | None = None
    minimum: float = 0
    maximum: float = 1
    step: float = 1
    options: dict[int, str] | None = None
    read_menu_id: int | None = None
    initial: float | None = None  # assumed until the cloud or a write says otherwise


# Names used by the first release, removed from the registry on upgrade.
LEGACY_MENU_NAMES = [
    f"{NAME} {n}"
    for n in (
        "PV DHW setpoint delta",
        "automatic winter mode zone 1",
        "automatic winter mode threshold zone 1",
        "automatic winter mode delay zone 1",
        "thermal sanitization cycle",
        "thermal sanitization frequency",
        "tank charge mode setting",
        "energy manager logic setting",
    )
]

MENU_SETTINGS: list[MenuSetting] = [
    MenuSetting(
        key="menu_6_15_2",
        name="PV DHW delta",
        portal_id="U6_15_2",
        kind="number",
        icon="mdi:solar-power",
        unit="°C",
        minimum=0,
        maximum=20,
        initial=5,
    ),
    MenuSetting(
        key="menu_6_3_5_0_0",
        name="winter auto z1",
        portal_id="U6_3_5_0_0",
        kind="switch",
        icon="mdi:snowflake",
        initial=0,
    ),
    MenuSetting(
        key="menu_6_3_5_0_1",
        name="winter auto threshold z1",
        portal_id="U6_3_5_0_1",
        kind="number",
        icon="mdi:thermometer",
        unit="°C",
        minimum=5,
        maximum=30,
        initial=15,
    ),
    MenuSetting(
        key="menu_6_3_5_0_2",
        name="winter auto delay z1",
        portal_id="U6_3_5_0_2",
        kind="number",
        icon="mdi:timer-outline",
        unit="min",
        minimum=0,
        maximum=1440,
        initial=300,
    ),
    MenuSetting(
        key="menu_6_9_5_0",
        name="sanitization",
        portal_id="U6_9_5_0",
        kind="switch",
        icon="mdi:shield-sync",
        initial=1,
    ),
    MenuSetting(
        key="menu_6_9_5_1",
        name="sanitization interval",
        portal_id="U6_9_5_1",
        kind="number",
        icon="mdi:calendar-sync",
        unit="h",
        minimum=24,
        maximum=2000,
        initial=480,
    ),
    MenuSetting(
        key="menu_6_9_3",
        name="tank charge mode",
        portal_id="U6_9_3",
        kind="select",
        icon="mdi:water-boiler",
        options={0: "Standard", 1: "Only HP", 2: "Fast"},
        read_menu_id=480,
    ),
    MenuSetting(
        key="menu_6_12_0",
        name="energy manager logic",
        portal_id="U6_12_0",
        kind="select",
        icon="mdi:leaf",
        options={0: "Max Saving", 1: "Max Ecology"},
        read_menu_id=455,
    ),
]


class _MenuBase(AristonEntity, RestoreEntity):
    """Shared logic: write through dataItems/set, read from menu items if possible."""

    def __init__(
        self,
        coordinator: DeviceDataUpdateCoordinator,
        extra: DeviceDataUpdateCoordinator | None,
        setting: MenuSetting,
    ) -> None:
        description = AristonBaseEntityDescription(
            key=setting.key,
            name=f"{NAME} {setting.name}",
            icon=setting.icon,
            entity_category=EntityCategory.CONFIG,
        )
        super().__init__(coordinator, description)
        self._setting = setting
        self._extra = extra
        self._value: float | None = None  # last written / restored value
        self._pending = False  # written and not yet confirmed by a new poll

    async def async_added_to_hass(self) -> None:
        """Restore the last value and follow the extra coordinator."""
        await super().async_added_to_hass()
        if self._extra is not None:
            self.async_on_remove(self._extra.async_add_listener(self._extra_updated))
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._value = self._from_state(last.state)

    def _extra_updated(self) -> None:
        self._pending = False
        self.async_write_ha_state()

    def _from_state(self, state: str) -> float | None:  # overridden
        try:
            return float(state)
        except ValueError:
            return None

    def _read(self) -> tuple[float | None, str | None]:
        """Return (value, source): cloud > last written/restored > initial."""
        if not self._pending and self._extra is not None:
            value = self._extra.portal_value(self._setting.portal_id)
            if value is not None:
                return value, "cloud"
            if self._setting.read_menu_id is not None:
                value = self._extra.menu_value(self._setting.read_menu_id)
                if value is not None:
                    return value, "cloud"
        if self._value is not None:
            return self._value, "last written"
        if self._setting.initial is not None:
            return self._setting.initial, "initial (assumed)"
        return None, None

    def _current(self) -> float | None:
        return self._read()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Tell where the shown value comes from."""
        return {"source": self._read()[1]}

    async def _write(self, value: float) -> None:
        try:
            await self.device.async_set_item_by_id(self._setting.portal_id, value, 0)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Could not set {self._setting.name}: {err!r}"
            ) from err
        self._value = value
        self._pending = True
        self.async_write_ha_state()


class MenuNumber(_MenuBase, NumberEntity):
    """Numeric menu setting."""

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, extra, setting: MenuSetting) -> None:
        super().__init__(coordinator, extra, setting)
        self._attr_native_min_value = setting.minimum
        self._attr_native_max_value = setting.maximum
        self._attr_native_step = setting.step
        self._attr_native_unit_of_measurement = setting.unit

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        return self._current()

    async def async_set_native_value(self, value: float) -> None:
        """Write the value."""
        await self._write(value)


class MenuSwitch(_MenuBase, SwitchEntity):
    """On/off menu setting."""

    def _from_state(self, state: str) -> float | None:
        return {"on": 1.0, "off": 0.0}.get(state)

    @property
    def is_on(self) -> bool | None:
        """Return True if on."""
        value = self._current()
        return None if value is None else value >= 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on."""
        await self._write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        await self._write(0)


class MenuSelect(_MenuBase, SelectEntity):
    """Enumerated menu setting."""

    def __init__(self, coordinator, extra, setting: MenuSetting) -> None:
        super().__init__(coordinator, extra, setting)
        self._attr_options = list((setting.options or {}).values())

    def _from_state(self, state: str) -> float | None:
        for key, label in (self._setting.options or {}).items():
            if label == state:
                return float(key)
        return None

    @property
    def current_option(self) -> str | None:
        """Return the selected option."""
        value = self._current()
        if value is None:
            return None
        return (self._setting.options or {}).get(int(value))

    async def async_select_option(self, option: str) -> None:
        """Write the option."""
        for key, label in (self._setting.options or {}).items():
            if label == option:
                await self._write(key)
                return
        raise HomeAssistantError(f"Unknown option {option}")


_CLASSES = {"number": MenuNumber, "switch": MenuSwitch, "select": MenuSelect}


def build_menu_entities(
    hass: HomeAssistant, entry: ConfigEntry, kind: str
) -> list[_MenuBase]:
    """Create the menu setting entities of one platform (Galevo only)."""
    data = hass.data[DOMAIN][entry.unique_id]
    coordinator = data.get(COORDINATOR)
    if not coordinator or getattr(coordinator.device, "system_type", None) != SystemType.GALEVO:
        return []
    extra = data.get(EXTRA_COORDINATOR)
    if not isinstance(extra, DeviceDataUpdateCoordinator):
        extra = None
    return [
        _CLASSES[kind](coordinator, extra, setting)
        for setting in MENU_SETTINGS
        if setting.kind == kind
    ]
