"""sensor.<raum>_heat_control: Solltemperatur des ersten aktiven Heizkreises; Attribut circuits = alle Kreise mit Zustand.
sensor.haus_heat_control: hausweiter Status (home/away/vacation/return); Attribute = Einstellungen, Quellen, Außen-Mittelwerte."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .manager import HeatManager


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    mgr: HeatManager = hass.data[DOMAIN]
    async_add_entities([RoomSensor(mgr, r) for r in mgr.rooms] + [HouseSensor(mgr)])


class _Base(SensorEntity):
    _attr_should_poll = False

    def __init__(self, mgr: HeatManager) -> None:
        self.mgr = mgr

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class RoomSensor(_Base):
    _attr_icon = "mdi:radiator"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _unrecorded_attributes = frozenset({"circuits", "version"})

    def __init__(self, mgr: HeatManager, room: str) -> None:
        super().__init__(mgr)
        self.room = room
        self._attr_name = f"{room.upper()} Heizungssteuerung"
        self._attr_unique_id = f"{DOMAIN}_{room}"
        self.entity_id = f"sensor.{room}_heat_control"

    @property
    def native_value(self) -> float | None:
        return self.mgr.room_state(self.room)

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.room_view(self.room)


class HouseSensor(_Base):
    _attr_icon = "mdi:home-thermometer"
    _unrecorded_attributes = frozenset({"settings", "facts", "outdoor", "version"})

    def __init__(self, mgr: HeatManager) -> None:
        super().__init__(mgr)
        self._attr_name = "Haus Heizungssteuerung"
        self._attr_unique_id = f"{DOMAIN}_haus"
        self.entity_id = "sensor.haus_heat_control"

    @property
    def native_value(self) -> str:
        f = self.mgr.facts or {}
        if f.get("returning"):
            return "return"
        if f.get("vacation"):
            return "vacation"
        if f.get("presence") is False:
            return "away"
        return "home"

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.house_view()
