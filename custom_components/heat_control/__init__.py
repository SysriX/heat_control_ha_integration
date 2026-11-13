"""Heizungssteuerung (heat_control) – Heiz- und Kühlkreise je Raum, bearbeitet im Raum-Panel
(Einstellungen → Heizungssteuerung). Einstellungen liegen in .storage/heat_control.

configuration.yaml:

    heat_control:
      rooms: [wz, bu, ku, sz, fl, ke, ba, bz]   # optional, das ist der Standard
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import CONF_ROOMS, DEFAULT_ROOMS, DOMAIN
from .manager import HeatManager

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Any(None, vol.Schema({
    vol.Optional(CONF_ROOMS, default=DEFAULT_ROOMS): vol.All(cv.ensure_list, [cv.slug]),
}))}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN) or {}
    mgr = HeatManager(hass, conf.get(CONF_ROOMS) or DEFAULT_ROOMS)
    hass.data[DOMAIN] = mgr
    websocket.async_register(hass)
    await mgr.async_start()

    async def _guard(coro) -> None:
        try:
            await coro
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def set_vacation(call: ServiceCall) -> None:
        patch = {"vacation_enabled": call.data["enabled"]}
        for k in ("from", "to"):
            if call.data.get(k):
                patch[f"vacation_{k}"] = call.data[k].strftime("%Y-%m-%dT%H:%M")
        await _guard(mgr.async_house(patch))

    async def set_enabled(call: ServiceCall) -> None:
        cid = call.data["circuit_id"]
        if cid not in mgr.circuits:
            raise ServiceValidationError(f"Kreis {cid} nicht gefunden")
        await _guard(mgr.async_save({"id": cid, "room": mgr.circuits[cid]["room"], "enabled": call.data["enabled"]}))

    async def reset_manual(call: ServiceCall) -> None:
        await _guard(mgr.async_reset_manual(call.data["circuit_id"]))

    hass.services.async_register(DOMAIN, "set_vacation", set_vacation, vol.Schema({
        vol.Required("enabled"): cv.boolean, vol.Optional("from"): cv.datetime, vol.Optional("to"): cv.datetime}))
    hass.services.async_register(DOMAIN, "set_enabled", set_enabled, vol.Schema({
        vol.Required("circuit_id"): cv.string, vol.Required("enabled"): cv.boolean}))
    hass.services.async_register(DOMAIN, "reset_manual", reset_manual, vol.Schema({vol.Required("circuit_id"): cv.string}))
    hass.async_create_task(async_load_platform(hass, "sensor", DOMAIN, {}, config))
    return True
