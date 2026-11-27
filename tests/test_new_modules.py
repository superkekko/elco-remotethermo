"""Tests for consumption, extra data, menu settings and resolver fix."""

import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ariston.ariston_api import ConnectionException
from ariston.const import SystemType
from homeassistant.exceptions import HomeAssistantError

from custom_components.ariston import _remove_legacy_consumption_sensors
from custom_components.ariston.const import (
    COORDINATOR,
    DOMAIN,
    EXTRA_COORDINATOR,
    EXTRA_STATE_DEVICE_METHOD,
    NAME,
)
from custom_components.ariston.consumption import (
    ARISTON_CONSUMPTION_SENSOR_TYPES,
    LIFETIME_ELEC,
    LIFETIME_GAS,
    EnergyDataUpdateCoordinator,
)
from custom_components.ariston.extra_data import ExtraDataUpdateCoordinator
from custom_components.ariston.menu_settings import (
    MENU_SETTINGS,
    MenuNumber,
    MenuSelect,
    MenuSwitch,
    build_menu_entities,
)
from custom_components.ariston import resolver_fix

SEQ = json.loads((Path(__file__).parent / "data/cons_sequences.json").read_text())


def _energy(hass, sequences=None, factor=10.69):
    device = SimpleNamespace(
        gateway="gw",
        name="n",
        consumptions_sequences=copy.deepcopy(SEQ if sequences is None else sequences),
        async_update_energy=AsyncMock(),
    )
    coord = EnergyDataUpdateCoordinator(hass, device, 600, "e", factor)
    coord._store = MagicMock()
    return coord, device


def _seq(device, k, p):
    return next(s for s in device.consumptions_sequences if s["k"] == k and s["p"] == p)


async def test_consumption_periods_and_conversion(hass):
    coord, _ = _energy(hass)
    assert coord.consumption_kwh((25, 21), "last 30 days") == 73.479
    assert coord.consumption_kwh((24, 20), "last 30 days") == 51.598
    assert coord.consumption_kwh((10,), "last 30 days") == 5.261
    assert coord.consumption((10,), "last 30 days", True) == round(5.261 / 10.69, 3)
    assert coord.total_kwh(((24, 20), (25, 21)), "last 30 days") == 125.077
    assert coord.total(((10,), (7,)), "last month", True) is not None
    for period in ("today", "yesterday", "last month"):
        assert coord.consumption_kwh((25, 21), period) is not None
    assert coord.consumption_kwh((25, 21), "nope") is None
    assert coord.consumption_kwh((99,), "today") is None
    assert coord.total_kwh(((99,),), "today") is None
    assert coord.total(((99,),), "today", True) is None
    assert coord.consumption((99,), "today", True) is None


async def test_bad_factor_and_missing_sequences(hass):
    coord, device = _energy(hass, factor="abc")
    assert coord.gas_kwh_per_smc == 10.69
    coord, device = _energy(hass, factor=-1)
    assert coord.gas_kwh_per_smc == 10.69
    device.consumptions_sequences = [{"k": 25, "p": 2, "v": "bad"}]
    assert coord._sequence((25,), 2) is None
    device.consumptions_sequences = []
    assert all(coord.provides(d) for d in ARISTON_CONSUMPTION_SENSOR_TYPES)


async def test_provides_follows_cloud_series(hass):
    coord, device = _energy(hass)
    assert all(coord.provides(d) for d in ARISTON_CONSUMPTION_SENSOR_TYPES)
    device.consumptions_sequences = [s for s in SEQ if s["k"] in (24, 25)]
    gas = [d for d in ARISTON_CONSUMPTION_SENSOR_TYPES if "gas" in d.key]
    assert gas and not any(coord.provides(d) for d in gas)
    assert coord.provides(SimpleNamespace())


async def test_sensor_getters(hass):
    coord, _ = _energy(hass)
    entity = SimpleNamespace(coordinator=coord)
    for description in ARISTON_CONSUMPTION_SENSOR_TYPES:
        if "since start" in description.key:
            coord._update_lifetime(dt.date(2026, 10, 31))
        description.get_native_value(entity)
        for extra in description.extra_states or []:
            extra[EXTRA_STATE_DEVICE_METHOD](entity)


async def test_lifetime_rollover_gap_and_persistence(hass):
    coord, device = _energy(hass)
    p4 = _seq(device, 25, 4)["v"]
    p3 = _seq(device, 25, 3)["v"]
    assert coord._update_lifetime(dt.date(2026, 10, 31))
    first = coord.lifetime_kwh(("dhw_elec",))
    assert first == round(sum(p4) + sum(p3[-31:]), 3)
    elec = coord.lifetime_kwh(LIFETIME_ELEC)
    assert coord.lifetime(LIFETIME_GAS, True) is not None
    assert coord.lifetime(LIFETIME_ELEC, False) == elec
    coord._update_lifetime(dt.date(2026, 10, 31))
    assert coord.lifetime_kwh(LIFETIME_ELEC) == elec

    _seq(device, 25, 4)["v"] = p4[1:] + [round(sum(p3[-31:]), 3)]
    _seq(device, 25, 3)["v"] = p3[1:] + [1.5]
    coord._update_lifetime(dt.date(2026, 11, 1))
    assert abs(coord.lifetime_kwh(("dhw_elec",)) - (first + 1.5)) < 0.01

    saved = json.loads(json.dumps(coord._lifetime))
    other, _ = _energy(hass)
    other._lifetime = saved
    before = other.lifetime_kwh(("dhw_elec",))
    other._update_lifetime(dt.date(2027, 2, 5))
    assert other.lifetime_kwh(("dhw_elec",)) >= before

    _seq(device, 25, 3)["v"] = [0.0] * 60
    coord._update_lifetime(dt.date(2026, 11, 20))
    assert coord.lifetime_kwh(("dhw_elec",)) >= first
    assert coord.lifetime_kwh(("nothing",)) is None


async def test_lifetime_without_data_and_storage(hass):
    coord, device = _energy(hass, sequences=[])
    assert coord._update_lifetime(dt.date(2026, 10, 31)) is False
    coord._store.async_load = AsyncMock(return_value={"series": {}})
    await coord.async_load_lifetime()
    assert coord._lifetime == {"series": {}}
    coord._store.async_load = AsyncMock(side_effect=OSError)
    await coord.async_load_lifetime()
    assert coord._lifetime == {}


async def test_energy_update_saves_lifetime(hass):
    coord, device = _energy(hass)
    await coord._async_update_data()
    coord._store.async_delay_save.assert_called_once()
    coord._update_lifetime = MagicMock(side_effect=RuntimeError)
    await coord._async_update_data()


# -- extra data ------------------------------------------------------------------


def _extra(hass, get, portal=("U6_15_2",)):
    device = SimpleNamespace(
        gateway="gw", name="n", api=SimpleNamespace(_async_get=get)
    )
    return ExtraDataUpdateCoordinator(
        hass, device, 600, "x", "https://x/api/v2", [455, 480, 455], list(portal)
    )


async def test_extra_fetch_and_accessors(hass):
    menu = [
        {"id": 455, "value": 1, "options": {"1": "Max Ecology"}, "unit": "°C"},
        {"id": 480, "value": 3276, "max": 3276, "min": 0},
        {"id": 481, "value": -3277, "max": 5, "min": -3277},
        {"id": 482, "value": None},
        {"id": 483, "value": 4, "nodeId": "U_6.15.2"},
        {"id": 484, "value": 7, "nodeId": "U_6.15.2"},
        "junk",
    ]

    async def get(url):
        if "plantData" in url or "remote" in url.lower():
            return {"a": 1}
        return menu

    coord = _extra(hass, get)
    coord.data = await coord._async_fetch()
    assert coord.menu_value(455) == 1
    assert coord.menu_value(480) is None
    assert coord.menu_value(481) is None
    assert coord.menu_value(482) is None
    assert coord.menu_value(999) is None
    assert coord.menu_text(455) == "Max Ecology"
    assert coord.menu_text(999) is None
    assert coord.menu_unit(455) == "°C"
    assert coord.menu_unit(999) is None
    assert coord.portal_value("U6_15_2") in (4, 7)
    assert coord.portal_value("nope") is None
    assert coord.plant_value("a") == 1
    assert coord.menu_text(483) == "4"
    assert coord.provides(SimpleNamespace(required_menu_item=None, required_plant_key=None))
    assert coord.provides(SimpleNamespace(required_menu_item=455, required_plant_key="a"))
    assert not coord.provides(SimpleNamespace(required_menu_item=1, required_plant_key=None))
    assert not coord.provides(SimpleNamespace(required_menu_item=None, required_plant_key="z"))


async def test_extra_partial_failures_keep_previous(hass):
    state = {"mode": "ok"}

    async def get(url):
        if state["mode"] == "menu":
            if "menuItems" in url:
                raise ConnectionException(500)
            return {"a": 1}
        if state["mode"] == "plant":
            if "menuItems" in url:
                return [{"id": 455, "value": 2}]
            raise ConnectionException(500)
        if state["mode"] == "all":
            raise ConnectionException(500)
        return [{"id": 455, "value": 1}] if "menuItems" in url else {"a": 1}

    coord = _extra(hass, get, portal=())
    coord.data = await coord._async_fetch()
    assert await coord._async_fetch_portal(None, "gw") is None
    state["mode"] = "menu"
    data = await coord._async_fetch()
    assert data["menu"] == coord.data["menu"]
    state["mode"] = "plant"
    data = await coord._async_fetch()
    assert data["plant"] == coord.data["plant"]
    state["mode"] = "all"
    with pytest.raises(ConnectionException):
        await coord._async_fetch()


async def test_extra_portal_failure_is_ignored(hass):
    async def get(url):
        if "U6_15_2" in url:
            raise ConnectionException(404)
        return {} if "plantData" in url.lower() or "remote" in url.lower() else []

    coord = _extra(hass, get)
    data = await coord._async_fetch()
    assert data["portal"] == {}


# -- menu settings ---------------------------------------------------------------


def _hass_with(hass, coordinator, extra, entry_id="e1"):
    hass.data[DOMAIN] = {entry_id: {COORDINATOR: coordinator, EXTRA_COORDINATOR: extra}}
    return SimpleNamespace(unique_id=entry_id, entry_id=entry_id)


def test_build_menu_entities(hass, coordinator):
    entry = _hass_with(hass, coordinator, {})
    assert len(build_menu_entities(hass, entry, "number")) == 4
    assert len(build_menu_entities(hass, entry, "switch")) == 2
    assert len(build_menu_entities(hass, entry, "select")) == 2
    coordinator.device.system_type = SystemType.VELIS
    assert build_menu_entities(hass, entry, "number") == []
    hass.data[DOMAIN]["e1"] = {}
    assert build_menu_entities(hass, entry, "number") == []


async def test_menu_entities_read_write_restore(hass, coordinator):
    coordinator.device.async_set_item_by_id = AsyncMock()
    extra = SimpleNamespace(
        portal_value=lambda pid: 7 if pid == "U6_15_2" else None,
        menu_value=lambda mid: 1 if mid == 480 else None,
    )
    by_key = {s.key: s for s in MENU_SETTINGS}
    number = MenuNumber(coordinator, extra, by_key["menu_6_15_2"])
    switch = MenuSwitch(coordinator, None, by_key["menu_6_3_5_0_0"])
    select = MenuSelect(coordinator, extra, by_key["menu_6_9_3"])
    other = MenuSelect(coordinator, None, by_key["menu_6_12_0"])
    for entity in (number, switch, select, other):
        entity.async_write_ha_state = MagicMock()

    assert number.native_value == 7
    assert number.extra_state_attributes == {"source": "cloud"}
    assert select.current_option == "Only HP"
    assert switch.is_on is False
    assert switch.extra_state_attributes == {"source": "initial (assumed)"}
    assert other.current_option is None

    await number.async_set_native_value(9)
    coordinator.device.async_set_item_by_id.assert_awaited_with("U6_15_2", 9, 0)
    assert number.native_value == 9
    assert number.extra_state_attributes == {"source": "last written"}
    number._extra_updated()
    assert number.native_value == 7

    await switch.async_turn_on()
    assert switch.is_on is True
    await switch.async_turn_off()
    assert switch.is_on is False
    await select.async_select_option("Fast")
    await other.async_select_option("Max Ecology")
    assert other.current_option == "Max Ecology"
    with pytest.raises(HomeAssistantError):
        await select.async_select_option("bogus")

    coordinator.device.async_set_item_by_id.side_effect = ConnectionException(500)
    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(1)

    assert number._from_state("12.5") == 12.5
    assert number._from_state("x") is None
    assert switch._from_state("on") == 1.0
    assert select._from_state("Fast") == 2.0
    assert select._from_state("zzz") is None


async def test_menu_entity_added_restores_state(hass, coordinator):
    extra = MagicMock()
    by_key = {s.key: s for s in MENU_SETTINGS}
    entity = MenuNumber(coordinator, extra, by_key["menu_6_15_2"])
    entity.hass = hass
    entity.entity_id = "number.x"
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="12"))
    entity.async_on_remove = MagicMock()
    await entity.async_added_to_hass()
    assert entity._value == 12
    extra.async_add_listener.assert_called_once()
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="unknown"))
    entity._value = None
    await entity.async_added_to_hass()
    assert entity._value is None


# -- legacy cleanup & resolver ---------------------------------------------------


async def test_legacy_cleanup(hass):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="u")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    def add(domain, uid):
        return registry.async_get_or_create(
            domain, DOMAIN, uid, config_entry=entry
        ).entity_id

    old = add("sensor", f"gw-{NAME} central heating gas consumption")
    pump = add(
        "sensor", f"gw-{NAME} domestic hot water heating pump electricity consumption"
    )
    menu = add("number", f"gw-{NAME} PV DHW setpoint delta")
    keep = add("sensor", "gw-something else")

    _remove_legacy_consumption_sensors(hass, entry, "gw", False)
    assert registry.async_get(old) is None and registry.async_get(menu) is None
    assert registry.async_get(pump) is not None and registry.async_get(keep)
    _remove_legacy_consumption_sensors(hass, entry, "gw", True)
    assert registry.async_get(pump) is None


async def test_resolver_fix(monkeypatch):
    from ariston import ariston_api

    monkeypatch.setattr(ariston_api, "aiohttp", ariston_api.aiohttp)
    resolver_fix.apply_system_resolver()
    patched = ariston_api.aiohttp
    resolver_fix.apply_system_resolver()
    assert ariston_api.aiohttp is patched
    assert patched.ClientError is not None
    session = patched.ClientSession()
    await session.close()
    session = patched.ClientSession(connector=patched.TCPConnector())
    await session.close()
