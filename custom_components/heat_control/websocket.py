"""WebSocket-API für das Raum-Panel (Einstellungen → Heizungssteuerung)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_save, ws_delete, ws_house, ws_averages, ws_reset_manual):
        websocket_api.async_register_command(hass, cmd)


async def _run(connection, msg, coro) -> None:
    try:
        res = await coro
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], {"result": res})


@websocket_api.websocket_command({vol.Required("type"): "heat_control/save", vol.Required("circuit"): dict})
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_save(msg["circuit"]))


@websocket_api.websocket_command({vol.Required("type"): "heat_control/delete", vol.Required("circuit_id"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_delete(msg["circuit_id"]))


@websocket_api.websocket_command({vol.Required("type"): "heat_control/house", vol.Required("settings"): dict})
@websocket_api.async_response
async def ws_house(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_house(msg["settings"]))


@websocket_api.websocket_command({vol.Required("type"): "heat_control/averages", vol.Required("entity_id"): str})
@websocket_api.async_response
async def ws_averages(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_averages(msg["entity_id"]))


@websocket_api.websocket_command({vol.Required("type"): "heat_control/reset_manual", vol.Required("circuit_id"): str})
@websocket_api.async_response
async def ws_reset_manual(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_reset_manual(msg["circuit_id"]))
