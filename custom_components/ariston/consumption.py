"""Consumption sensors computed from the Ariston cloud consumption sequences.

The cloud (``consSequencesApi8``) returns, for every consumption type ``k``,
several sequences ``p``:

* p=2: 7 daily values, the last one is *today* (partial)
* p=3: 60 daily values, the last one is *today* (partial)
* p=4: monthly values, the last one is the last *complete* month

The old library accessors only returned the last element of the hourly
sequence (a single hour), which is why the old sensors looked wrong.

Cloud values are always energy in kWh, also for gas. Gas is converted to
standard cubic metres (Smc, m3) with a configurable factor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from typing import Any

from ariston.const import DeviceFeatures, SystemType
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ENERGY_COORDINATOR,
    EXTRA_STATE_ATTRIBUTE,
    EXTRA_STATE_DEVICE_METHOD,
    NAME,
    AristonSensorEntityDescription,
)
from .coordinator import DeviceDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

LIFETIME_SERIES = {
    "ch_elec": (24, 20),
    "dhw_elec": (25, 21),
    "ch_gas": (7,),
    "dhw_gas": (10,),
}
LIFETIME_ELEC = ("ch_elec", "dhw_elec")
LIFETIME_GAS = ("ch_gas", "dhw_gas")

DEFAULT_GAS_KWH_PER_SMC = 10.69
GAS_KWH_PER_SMC: str = "gas_kwh_per_smc"

# Cloud consumption types (first one found wins).
SERIES_CH_ELECTRICITY = (24, 20)
SERIES_DHW_ELECTRICITY = (25, 21)
SERIES_CH_GAS = (7,)
SERIES_DHW_GAS = (10,)

PERIOD_TODAY = "today"
PERIOD_YESTERDAY = "yesterday"
PERIOD_30_DAYS = "last 30 days"
PERIOD_LAST_MONTH = "last month"

# Names of the old (wrong) consumption sensors, removed from the registry.
LEGACY_SENSOR_NAMES = [
    f"{NAME} gas consumption for heating last month",
    f"{NAME} electricity consumption for heating last month",
    f"{NAME} electricity consumption for cooling last month",
    f"{NAME} gas consumption for water last month",
    f"{NAME} electricity consumption for water last month",
    f"{NAME} central heating total energy consumption",
    f"{NAME} domestic hot water total energy consumption",
    f"{NAME} central heating gas consumption",
    f"{NAME} domestic hot water gas consumption",
    f"{NAME} central heating electricity consumption",
    f"{NAME} domestic hot water electricity consumption",
]


# Still valid on Lydos/VELIS systems, wrong (single hour) on Galevo.
LEGACY_GALEVO_ONLY_NAMES = [
    f"{NAME} domestic hot water heating pump electricity consumption",
    f"{NAME} domestic hot water resistor electricity consumption",
]


class EnergyDataUpdateCoordinator(DeviceDataUpdateCoordinator):
    """Energy coordinator: polls consumption sequences and derives the periods."""

    def __init__(
        self,
        hass: HomeAssistant,
        device,
        scan_interval_seconds: int,
        coordinator_name: str,
        gas_kwh_per_smc: float = DEFAULT_GAS_KWH_PER_SMC,
        config_entry=None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            device,
            scan_interval_seconds,
            coordinator_name,
            device.async_update_energy,
            config_entry=config_entry,
        )
        try:
            factor = float(gas_kwh_per_smc)
        except (TypeError, ValueError):
            factor = DEFAULT_GAS_KWH_PER_SMC
        self.gas_kwh_per_smc = factor if factor > 0 else DEFAULT_GAS_KWH_PER_SMC
        self._store: Store = Store(hass, 1, f"ariston_lifetime_{device.gateway}")
        self._lifetime: dict[str, Any] = {}

    # -- lifetime totals -------------------------------------------------------
    # The cloud only keeps ~21 months of history, so a "since the beginning"
    # counter is built here: closed months are added once to a persisted base,
    # the current month is added from the daily values. It never decreases.

    async def async_load_lifetime(self) -> None:
        """Load the persisted lifetime counters."""
        try:
            self._lifetime = await self._store.async_load() or {}
        except Exception:  # noqa: BLE001 - corrupt storage must not block setup
            _LOGGER.warning("Could not read lifetime totals, starting over")
            self._lifetime = {}

    async def _async_update_data(self):
        """Poll the cloud, then update the lifetime counters."""
        result = await super()._async_update_data()
        try:
            if self._update_lifetime(dt_util.now().date()):
                self._store.async_delay_save(lambda: self._lifetime, 10)
        except Exception:  # noqa: BLE001 - never fail the poll for this
            _LOGGER.exception("Lifetime totals update failed")
        return result

    def _update_lifetime(self, today) -> bool:
        """Advance the counters; return True if the stored state changed."""
        changed = False
        this_month = today.year * 12 + today.month - 1
        last_closed = this_month - 1
        series_state = self._lifetime.setdefault("series", {})
        for name, kinds in LIFETIME_SERIES.items():
            months = self._sequence(kinds, 4)
            days = self._sequence(kinds, 3)
            if months is None or days is None:
                continue
            state = series_state.get(name)
            if state is None:
                state = {"base": round(sum(months), 3), "through": last_closed}
                series_state[name] = state
                changed = True
            elif state["through"] < last_closed:
                missing = min(last_closed - state["through"], len(months))
                state["base"] = round(state["base"] + sum(months[-missing:]), 3)
                state["through"] = last_closed
                changed = True
            state["month_to_date"] = round(sum(days[-today.day :]), 3)
            state["total"] = round(max(state.get("total", 0.0), state["base"] + state["month_to_date"]), 3)
            changed = True
        return changed

    def lifetime_kwh(self, names) -> float | None:
        """Lifetime total of the given series, in kWh."""
        state = self._lifetime.get("series", {})
        values = [state[n]["total"] for n in names if n in state and "total" in state[n]]
        return round(sum(values), 3) if values else None

    def lifetime(self, names, gas: bool) -> float | None:
        """Lifetime total in the sensor unit (kWh, or m3 for gas)."""
        kwh = self.lifetime_kwh(names)
        if kwh is None or not gas:
            return kwh
        return round(kwh / self.gas_kwh_per_smc, 3)

    # -- raw access ------------------------------------------------------------

    def _sequence(self, kinds: Iterable[int], period: int) -> list[float] | None:
        sequences = getattr(self.device, "consumptions_sequences", None) or []
        for kind in kinds:
            for sequence in sequences:
                if sequence.get("k") == kind and sequence.get("p") == period:
                    values = sequence.get("v")
                    if isinstance(values, list):
                        return [v for v in values if isinstance(v, (int, float))]
        return None

    def has_series(self, kinds: Iterable[int]) -> bool:
        """Return True if the cloud returned this consumption type."""
        return any(self._sequence(kinds, p) is not None for p in (2, 3, 4))

    def consumption_kwh(self, kinds: Iterable[int], period: str) -> float | None:
        """Return the consumption of the period in kWh, or None if unknown."""
        if period == PERIOD_TODAY:
            values = self._sequence(kinds, 2)
            value = values[-1] if values else None
        elif period == PERIOD_YESTERDAY:
            values = self._sequence(kinds, 2)
            value = values[-2] if values and len(values) >= 2 else None
        elif period == PERIOD_30_DAYS:
            values = self._sequence(kinds, 3)
            value = sum(values[-30:]) if values else None
        elif period == PERIOD_LAST_MONTH:
            values = self._sequence(kinds, 4)
            value = values[-1] if values else None
        else:
            value = None
        return None if value is None else round(value, 3)

    def consumption(self, kinds: Iterable[int], period: str, gas: bool) -> float | None:
        """Return the value in the sensor unit (kWh, or m3 for gas)."""
        kwh = self.consumption_kwh(kinds, period)
        if kwh is None or not gas:
            return kwh
        return round(kwh / self.gas_kwh_per_smc, 3)

    def total_kwh(self, groups, period: str) -> float | None:
        """Sum of several consumption types (e.g. heating + hot water), in kWh."""
        values = [self.consumption_kwh(g, period) for g in groups]
        values = [v for v in values if v is not None]
        return round(sum(values), 3) if values else None

    def total(self, groups, period: str, gas: bool) -> float | None:
        """Total in the sensor unit (kWh, or m3 for gas)."""
        kwh = self.total_kwh(groups, period)
        if kwh is None or not gas:
            return kwh
        return round(kwh / self.gas_kwh_per_smc, 3)

    def provides(self, description) -> bool:
        """Create the sensor only if the cloud returns that consumption type."""
        names = getattr(description, "lifetime_names", None)
        if names is not None:
            if not getattr(self.device, "consumptions_sequences", None):
                return True
            return any(self.has_series(LIFETIME_SERIES[n]) for n in names)
        groups = getattr(description, "consumption_groups", None)
        if groups is not None:
            if not getattr(self.device, "consumptions_sequences", None):
                return True
            return any(self.has_series(g) for g in groups)
        kinds = getattr(description, "consumption_kinds", None)
        if kinds is None:
            return True
        sequences = getattr(self.device, "consumptions_sequences", None)
        if not sequences:
            return True  # not loaded yet: create it, it will fill in later
        return self.has_series(kinds)


@dataclass(kw_only=True, frozen=True)
class AristonConsumptionDescription(AristonSensorEntityDescription):
    """Sensor description carrying the consumption type(s) it reads."""

    consumption_kinds: tuple[int, ...] | None = None
    consumption_groups: tuple[tuple[int, ...], ...] | None = None
    lifetime_names: tuple[str, ...] | None = None


def _build() -> list[AristonSensorEntityDescription]:
    descriptions: list[AristonSensorEntityDescription] = []
    series = [
        ("central heating electricity", SERIES_CH_ELECTRICITY, False),
        ("domestic hot water electricity", SERIES_DHW_ELECTRICITY, False),
        ("central heating gas", SERIES_CH_GAS, True),
        ("domestic hot water gas", SERIES_DHW_GAS, True),
    ]
    for label, kinds, gas in series:
        for period in (PERIOD_TODAY, PERIOD_YESTERDAY, PERIOD_30_DAYS, PERIOD_LAST_MONTH):
            description = AristonConsumptionDescription(
                key=f"consumption {label} {period}",
                name=f"{NAME} {label} {period}",
                icon="mdi:fire" if gas else "mdi:flash",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_class=SensorDeviceClass.GAS if gas else SensorDeviceClass.ENERGY,
                native_unit_of_measurement=(
                    UnitOfVolume.CUBIC_METERS if gas else UnitOfEnergy.KILO_WATT_HOUR
                ),
                suggested_display_precision=3 if gas else 2,
                # Only "today" grows during the day and resets at midnight.
                state_class=(
                    SensorStateClass.TOTAL_INCREASING if period == PERIOD_TODAY else None
                ),
                device_features=[DeviceFeatures.HAS_METERING],
                consumption_kinds=kinds,
                coordinator=ENERGY_COORDINATOR,
                system_types=[SystemType.GALEVO],
                get_native_value=lambda entity, k=kinds, p=period, g=gas: entity.coordinator.consumption(
                    k, p, g
                ),
                extra_states=(
                    [
                        {
                            EXTRA_STATE_ATTRIBUTE: "kwh",
                            EXTRA_STATE_DEVICE_METHOD: lambda entity, k=kinds, p=period: entity.coordinator.consumption_kwh(
                                k, p
                            ),
                        }
                    ]
                    if gas
                    else None
                ),
            )
            descriptions.append(description)
    # Totals (heating + hot water). The app's monthly bar is the "last 30 days".
    totals = [
        ("total electricity", (SERIES_CH_ELECTRICITY, SERIES_DHW_ELECTRICITY), False),
        ("total gas", (SERIES_CH_GAS, SERIES_DHW_GAS), True),
    ]
    for label, groups, gas in totals:
        for period in (PERIOD_30_DAYS, PERIOD_LAST_MONTH):
            descriptions.append(
                AristonConsumptionDescription(
                    key=f"consumption {label} {period}",
                    name=f"{NAME} {label} {period}",
                    icon="mdi:fire" if gas else "mdi:flash",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_class=SensorDeviceClass.GAS if gas else SensorDeviceClass.ENERGY,
                    native_unit_of_measurement=(
                        UnitOfVolume.CUBIC_METERS if gas else UnitOfEnergy.KILO_WATT_HOUR
                    ),
                    suggested_display_precision=3 if gas else 2,
                    device_features=[DeviceFeatures.HAS_METERING],
                    consumption_groups=groups,
                    coordinator=ENERGY_COORDINATOR,
                    system_types=[SystemType.GALEVO],
                    get_native_value=lambda entity, g=groups, p=period, x=gas: entity.coordinator.total(
                        g, p, x
                    ),
                    extra_states=(
                        [
                            {
                                EXTRA_STATE_ATTRIBUTE: "kwh",
                                EXTRA_STATE_DEVICE_METHOD: lambda entity, g=groups, p=period: entity.coordinator.total_kwh(
                                    g, p
                                ),
                            }
                        ]
                        if gas
                        else None
                    ),
                )
            )
    for label, names, gas in [
        ("total electricity since start", LIFETIME_ELEC, False),
        ("total gas since start", LIFETIME_GAS, True),
    ]:
        descriptions.append(
            AristonConsumptionDescription(
                key=f"consumption {label}",
                name=f"{NAME} {label}",
                icon="mdi:fire" if gas else "mdi:flash",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_class=SensorDeviceClass.GAS if gas else SensorDeviceClass.ENERGY,
                native_unit_of_measurement=(
                    UnitOfVolume.CUBIC_METERS if gas else UnitOfEnergy.KILO_WATT_HOUR
                ),
                suggested_display_precision=3 if gas else 2,
                state_class=SensorStateClass.TOTAL_INCREASING,
                device_features=[DeviceFeatures.HAS_METERING],
                lifetime_names=names,
                coordinator=ENERGY_COORDINATOR,
                system_types=[SystemType.GALEVO],
                get_native_value=lambda entity, n=names, x=gas: entity.coordinator.lifetime(n, x),
                extra_states=(
                    [
                        {
                            EXTRA_STATE_ATTRIBUTE: "kwh",
                            EXTRA_STATE_DEVICE_METHOD: lambda entity, n=names: entity.coordinator.lifetime_kwh(n),
                        }
                    ]
                    if gas
                    else None
                ),
            )
        )
    return descriptions


ARISTON_CONSUMPTION_SENSOR_TYPES: list[AristonSensorEntityDescription] = _build()
