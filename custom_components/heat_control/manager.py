"""Heizungssteuerung: Heiz- und Kühlkreise je Raum.

Ein Kreis ist ein oder mehrere climate-Geräte mit eigenem Satz Temperaturen. Heizkreise (Thermostate)
und Kühlkreise (Klimaanlagen, wahlweise Wärmepumpe) laufen getrennt.

Je Kreis entsteht in jedem Durchlauf (jede Minute und sofort bei Änderung einer Quelle) ein Wunsch:
  Status   zuhause / abwesend (nach Verzögerung) / Urlaub / Rückkehr (Vorheizen vor Urlaubsende)
  Basis    Tageszeit-Temperatur oder Festwert, abwesend, Urlaub
  Sperren  Aus, Fenster offen, Heizgrenze (Außentemperatur aktuell oder Mittel 1 T/1 W/1 M/1 J),
           Wärmepumpe heizt, Klimaanlage kühlt → Frostschutz bzw. Klima aus
  Hand     Ändert jemand den Sollwert am Gerät/Dashboard, gilt der Wert bis zur nächsten Tageszeit
           bzw. bis sich der Status ändert (oder X Stunden).
Gesendet wird funkschonend: nur bei Abweichung, höchstens ein Befehl je Gerät pro Minute, mit Rückmeldung.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    AVG_REFRESH_S, AWAY_COOL, BASES, BASIS_DAYS, COMP_MIN_GAP_S, CONFIRM_S, DAYTIME_START, DEFAULT_CIRCUIT,
    DEFAULT_HOUSE, DEFAULT_TEMPS_COOL, DEFAULT_TEMPS_HEAT, DEFAULT_TIMES, EVENT_CHANGED, HEAT_BY,
    HEAT_SENSORS_HELPER, HOLD, KINDS, LINKS, MAX_CIRCUITS_PER_ROOM, MAX_TRIES, MODES, PERIOD_NAMES, PERIODS,
    RETRY_LATE_S, RETURN_GRACE_H, ROLES, SEND_MIN_GAP_S, SIGNAL_UPDATE, SRC_ALARM, SRC_PRESENCE, STORE_KEY,
    STORE_VERSION, TICK_SECONDS, VERSION,
)

_LOGGER = logging.getLogger(__name__)

RE_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RE_ENT = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
RE_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
BAD = ("unknown", "unavailable", "", None)
TEMP_KEYS = ["night", "morning", "midday", "afternoon", "evening", "fixed", "away", "vacation", "frost"]
STATUS_NAMES = {"home": "Zuhause", "away": "Abwesend", "vacation": "Urlaub", "return": "Rückkehr"}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _fmt(v: float | None) -> str:
    return "–" if v is None else f"{v:.1f}".replace(".", ",") + " °C"


def _iso(d: datetime | None) -> str | None:
    return dt_util.as_local(d).isoformat() if d else None


def _parse(s: str | None) -> datetime | None:
    d = dt_util.parse_datetime(s) if s else None
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return d


def period_at(times: dict[str, str], now: datetime) -> tuple[str, datetime]:
    """(aktuelle Tageszeit, Beginn der nächsten)."""
    marks = sorted((int(t[:2]) * 60 + int(t[3:]), k) for k, t in times.items())
    m = now.hour * 60 + now.minute
    cur = marks[-1][1]
    for mm, k in marks:
        if mm <= m:
            cur = k
    nxt = next(((mm, k) for mm, k in marks if mm > m), None)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if nxt is None:
        nxt_dt = day + timedelta(days=1, minutes=marks[0][0])
    else:
        nxt_dt = day + timedelta(minutes=nxt[0])
    return cur, nxt_dt


class HeatManager:
    def __init__(self, hass: HomeAssistant, rooms: list[str]) -> None:
        self.hass = hass
        self.rooms = rooms
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.house: dict = dict(DEFAULT_HOUSE)
        self.circuits: dict[str, dict] = {}
        self.rt: dict[str, dict] = {}          # Kreis → {nh, manual, season_heat, season_cool, last_hvac, comp, win}
        self.dev: dict[str, dict] = {}         # Gerät → {t, hvac, at, tries, ok}
        self.away_since: str | None = None
        self.view: dict[str, dict] = {}        # Kreis → zuletzt berechneter Zustand (für Sensoren/Panel)
        self.facts: dict = {}
        self.avg: dict[str, dict] = {}         # Sensor → Basis → {v, days, at}
        self._avg_busy: set[tuple[str, str]] = set()
        self.root = Context()
        self._unsubs: list = []
        self._watch: set[str] = set()
        self._busy = False
        self._again = False

    # ================================================================ Lebenszyklus
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.house = self._clean_house({**DEFAULT_HOUSE, **(data.get("house") or {})})
        for k, v in (data.get("circuits") or {}).items():
            try:
                self.circuits[k] = self._clean({**DEFAULT_CIRCUIT, **v}, k)
            except ValueError as err:
                _LOGGER.warning("Kreis %s verworfen: %s", k, err)
        self.rt = data.get("rt") or {}
        self.dev = data.get("dev") or {}
        self.away_since = data.get("away_since")
        if not data.get("seeded"):
            self._seed()
        self._unsubs.append(async_track_time_change(self.hass, self._tick, second=TICK_SECONDS))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state, event_filter=self._is_watched))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        self._rewatch()
        self.hass.async_create_task(self._process())

    def _seed(self) -> None:
        """Erster Start: je Raum mit climate.<raum>_heater_grp einen (noch inaktiven) Heizkreis anlegen."""
        for r in self.rooms:
            grp = f"climate.{r}_heater_grp"
            if self.hass.states.get(grp) is None:
                continue
            c = self._clean({**DEFAULT_CIRCUIT, "room": r, "name": "Heizkörper", "devices": [grp],
                             "sensor_in": self._helper_sensor(r)})
            self.circuits[c["id"]] = c
        self._save()

    def _helper_sensor(self, room: str) -> str:
        s = self.hass.states.get(HEAT_SENSORS_HELPER.format(room=room))
        for part in (s.state if s else "").split(";"):
            k, _, v = part.partition("=")
            if k.strip() == "current" and RE_ENT.match(v.strip()):
                return v.strip()
        return ""

    async def _async_stop(self, _ev: Event | None = None) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self.store.async_save(self._data())

    def _data(self) -> dict:
        return {"house": self.house, "circuits": self.circuits, "rt": self.rt, "dev": self.dev,
                "away_since": self.away_since, "seeded": True}

    def _save(self) -> None:
        self.store.async_delay_save(self._data, 5)

    # ================================================================ Bereinigen
    def _num(self, d: dict, k: str, lo: float, hi: float, dflt: float) -> float:
        v = _f(d.get(k, dflt))
        return dflt if v is None else max(lo, min(hi, v))

    def _clean_limit(self, raw: Any, dflt: dict, cool: bool) -> dict:
        x = raw if isinstance(raw, dict) else {}
        on, off = self._num(x, "on", -30, 40, dflt["on"]), self._num(x, "off", -30, 40, dflt["off"])
        if not cool and off <= on:
            raise ValueError(f"Heizgrenze: „aus über“ ({off:g} °C) muss größer sein als „an unter“ ({on:g} °C)")
        if cool and off >= on:
            raise ValueError(f"Kühlgrenze: „aus unter“ ({off:g} °C) muss kleiner sein als „an über“ ({on:g} °C)")
        return {"enabled": bool(x.get("enabled", dflt["enabled"])),
                "basis": x.get("basis") if x.get("basis") in BASES else dflt["basis"], "on": on, "off": off}

    def _clean(self, c: dict, cid: str | None = None) -> dict:
        room = str(c.get("room") or "")
        if room not in self.rooms:
            raise ValueError(f"Unbekannter Raum: {room!r}")
        kind = c.get("kind") if c.get("kind") in KINDS else "heat"
        dt = DEFAULT_TEMPS_HEAT if kind == "heat" else DEFAULT_TEMPS_COOL
        traw = c.get("temps") if isinstance(c.get("temps"), dict) else {}
        temps = {k: round(self._num(traw, k, 4.5, 35, dt[k]) * 2) / 2 for k in TEMP_KEYS}
        traw = c.get("times") if isinstance(c.get("times"), dict) else {}
        times = {k: str(traw.get(k)) if RE_HM.match(str(traw.get(k) or "")) else DEFAULT_TIMES[k] for k in PERIODS}
        if len(set(times.values())) < len(times):
            raise ValueError("Zwei Tageszeiten beginnen zur gleichen Uhrzeit")
        ents = lambda key, doms: [e for e in dict.fromkeys(c.get(key) or [])  # noqa: E731
                                  if isinstance(e, str) and RE_ENT.match(e) and e.split(".")[0] in doms]
        sens = lambda key: str(c.get(key) or "") if RE_ENT.match(str(c.get(key) or "")) else ""  # noqa: E731
        dl = DEFAULT_CIRCUIT
        out = {
            "id": cid or c.get("id") or uuid.uuid4().hex[:10],
            "room": room,
            "kind": kind,
            "name": (str(c.get("name") or "").strip() or ("Heizkörper" if kind == "heat" else "Klimaanlage"))[:40],
            "enabled": bool(c.get("enabled")),
            "devices": ents("devices", ("climate",))[:12],
            "mode": c.get("mode") if c.get("mode") in MODES else "schedule",
            "temps": temps,
            "own_times": bool(c.get("own_times")),
            "times": times,
            "use_presence": bool(c.get("use_presence", True)),
            "use_alarm": bool(c.get("use_alarm", True)),
            "link": c.get("link") if c.get("link") in LINKS else "or",
            "sensor_in": sens("sensor_in"),
            "sensor_out": sens("sensor_out"),
            "compensate": bool(c.get("compensate")),
            "limit": self._clean_limit(c.get("limit"), dl["limit"], False),
            "windows": ents("windows", ("binary_sensor", "sensor", "input_boolean"))[:16],
            "window_delay_s": int(self._num(c, "window_delay_s", 0, 1800, 30)),
            "window_tilted": bool(c.get("window_tilted", True)),
            "hold": c.get("hold") if c.get("hold") in HOLD else "period",
            "hold_hours": self._num(c, "hold_hours", 0.5, 48, 2),
            "role": c.get("role") if c.get("role") in ROLES else "cool",
            "heat_by": c.get("heat_by") if c.get("heat_by") in HEAT_BY else "thermostats",
            "both_below": self._num(c, "both_below", -30, 30, 5),
            "hysteresis": self._num(c, "hysteresis", 0.2, 3, 0.5),
            "heat_ref": str(c.get("heat_ref") or ""),
            "follow_comfort": bool(c.get("follow_comfort")),
            "away_cool": c.get("away_cool") if c.get("away_cool") in AWAY_COOL else "temp",
            "cool_limit": self._clean_limit(c.get("cool_limit"), dl["cool_limit"], True),
            "heat_limit": self._clean_limit(c.get("heat_limit"), dl["heat_limit"], False),
        }
        if out["temps"]["frost"] > min(out["temps"][k] for k in TEMP_KEYS if k != "frost") and kind == "heat":
            raise ValueError("Frostschutz muss unter allen anderen Temperaturen liegen")
        return out

    def _clean_house(self, h: dict) -> dict:
        out = {
            "outdoor_sensor": str(h.get("outdoor_sensor") or "") if RE_ENT.match(str(h.get("outdoor_sensor") or "")) else "",
            "away_delay_min": int(self._num(h, "away_delay_min", 0, 240, 15)),
            "vacation_enabled": bool(h.get("vacation_enabled")),
            "vacation_from": str(h.get("vacation_from") or "") if RE_DT.match(str(h.get("vacation_from") or "")) else "",
            "vacation_to": str(h.get("vacation_to") or "") if RE_DT.match(str(h.get("vacation_to") or "")) else "",
            "vacation_preheat_h": self._num(h, "vacation_preheat_h", 0, 48, 3),
            "vacation_alarm": bool(h.get("vacation_alarm", True)),
            "vacation_away_h": self._num(h, "vacation_away_h", 0, 720, 24),
        }
        if out["vacation_enabled"]:
            if not out["vacation_from"] or not out["vacation_to"]:
                raise ValueError("Urlaub: Beginn und Ende angeben")
            if out["vacation_to"] <= out["vacation_from"]:
                raise ValueError("Urlaub: Ende muss nach dem Beginn liegen")
        return out

    # ================================================================ Bearbeiten (WebSocket/Dienste)
    async def async_save(self, patch: dict) -> str:
        cid = patch.get("id")
        if cid and cid not in self.circuits:
            cid = None
        if not cid and sum(1 for c in self.circuits.values() if c["room"] == patch.get("room")) >= MAX_CIRCUITS_PER_ROOM:
            raise ValueError(f"Höchstens {MAX_CIRCUITS_PER_ROOM} Kreise je Raum")
        old = self.circuits.get(cid) or {}
        base = {**DEFAULT_CIRCUIT, **old, **patch}
        if not cid and patch.get("kind") == "cool" and "temps" not in patch:
            base["temps"] = DEFAULT_TEMPS_COOL
            base["name"] = patch.get("name") or "Klimaanlage"
        c = self._clean(base, cid)
        self.circuits[c["id"]] = c
        rt = self.rt.setdefault(c["id"], {})
        rt.pop("manual", None)                  # neue Einstellungen → Hand-Wert verwerfen
        rt.pop("comp", None)
        for e in set(self._devices(old)) | set(self._devices(c)):
            self.dev.pop(e, None)               # neu senden dürfen
        self._save()
        self._rewatch()
        await self._process()
        return c["id"]

    async def async_delete(self, cid: str) -> None:
        if self.circuits.pop(cid, None) is None:
            raise ValueError("Kreis nicht gefunden")
        self.rt.pop(cid, None)
        self.view.pop(cid, None)
        self._save()
        self._rewatch()
        self._push()

    async def async_house(self, patch: dict) -> dict:
        self.house = self._clean_house({**self.house, **patch})
        self._save()
        self._rewatch()
        await self._process()
        return self.house

    async def async_reset_manual(self, cid: str) -> None:
        if cid not in self.circuits:
            raise ValueError("Kreis nicht gefunden")
        self.rt.setdefault(cid, {}).pop("manual", None)
        for e in self._devices(self.circuits[cid]):
            self.dev.pop(e, None)
        self._save()
        await self._process()

    async def async_averages(self, entity: str) -> dict:
        """Mittelwerte eines Außensensors für die Anzeige (wartet auf die Statistik)."""
        if not RE_ENT.match(entity or ""):
            raise ValueError("Ungültiger Sensor")
        for b in BASIS_DAYS:
            await self._refresh_avg(entity, b, force=False)
        return self._avg_view(entity)

    # ================================================================ Quellen
    def _state(self, eid: str | None):
        return self.hass.states.get(eid) if eid else None

    def _txt(self, eid: str) -> str:
        s = self._state(eid)
        return s.state.strip() if s and s.state not in BAD else ""

    def _devices(self, c: dict) -> list[str]:
        """Gerätelisten auflösen: climate-Gruppen → einzelne Geräte (sonst würde doppelt gesendet)."""
        out: list[str] = []

        def add(e: str, depth: int) -> None:
            s = self._state(e)
            # climate_group_helper: member_entities, HA-Gruppen: entity_id
            members = (s.attributes.get("member_entities") or s.attributes.get("entity_id") or []) if s else []
            if members and depth < 3:
                for x in members:
                    add(x, depth + 1)
            elif e not in out:
                out.append(e)
        for e in c.get("devices") or []:
            add(e, 0)
        return out

    def presence(self) -> tuple[bool | None, str]:
        e = self._txt(SRC_PRESENCE) or "zone.home"
        s = self._state(e)
        if not s or s.state in BAD:
            return None, e
        if e.startswith("zone."):
            return (s.state.isdigit() and int(s.state) > 0), e
        return s.state in ("on", "home", "at_home", "true"), e

    def alarm(self) -> tuple[str | None, str]:
        e = self._txt(SRC_ALARM)
        s = self._state(e)
        return (s.state if s and s.state not in BAD else None), e

    def outdoor_entity(self, c: dict) -> str:
        return c.get("sensor_out") or self.house["outdoor_sensor"]

    def outdoor(self, c: dict, basis: str) -> float | None:
        e = self.outdoor_entity(c)
        if basis == "now":
            s = self._state(e)
            return _f(s.state) if s else None
        a = (self.avg.get(e) or {}).get(basis)
        return a["v"] if a else None

    def room_temp(self, c: dict) -> float | None:
        if c.get("sensor_in"):
            s = self._state(c["sensor_in"])
            v = _f(s.state) if s else None
            if v is not None:
                return v
        for e in self._devices(c):
            s = self._state(e)
            v = _f(s.attributes.get("current_temperature")) if s else None
            if v is not None:
                return v
        return None

    def window_open(self, c: dict, now: datetime) -> tuple[bool, str | None]:
        """(offen nach Verzögerung, erster offener Kontakt)."""
        first: datetime | None = None
        who = None
        for e in c.get("windows") or []:
            s = self._state(e)
            if not s:
                continue
            st = s.state.lower()
            is_open = st in ("on", "open", "offen", "true") or (c.get("window_tilted", True) and st in ("tilted", "gekippt"))
            if is_open and (first is None or s.last_changed < first):
                first, who = s.last_changed, e
        if first is None:
            return False, None
        return (now - first).total_seconds() >= c.get("window_delay_s", 30), who

    def times(self, c: dict) -> dict[str, str]:
        if c.get("own_times"):
            return c["times"]
        out = {}
        for k in PERIODS:
            v = self._txt(DAYTIME_START.format(k))[:5]
            out[k] = v if RE_HM.match(v) else DEFAULT_TIMES[k]
        if len(set(out.values())) < len(out):
            return dict(DEFAULT_TIMES)
        return out

    # ================================================================ Außen-Mittelwerte (Recorder-Statistik)
    async def _refresh_avg(self, entity: str, basis: str, force: bool = False) -> None:
        now = dt_util.utcnow()
        cur = (self.avg.get(entity) or {}).get(basis)
        if cur and not force and (now - _parse(cur["at"])).total_seconds() < AVG_REFRESH_S[basis]:
            return
        if (entity, basis) in self._avg_busy:
            return
        self._avg_busy.add((entity, basis))
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period
            days = BASIS_DAYS[basis]
            start = now - timedelta(days=days)
            period = "hour" if days <= 7 else "day"
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, now, {entity}, period, None, {"mean"})
            rows = [r for r in stats.get(entity, []) if r.get("mean") is not None]
            if rows:
                first = rows[0]["start"]
                first = first if isinstance(first, datetime) else dt_util.utc_from_timestamp(first)
                cover = min(days, (now - first).total_seconds() / 86400)
                v = sum(r["mean"] for r in rows) / len(rows)
                self.avg.setdefault(entity, {})[basis] = {"v": round(v, 1), "days": round(cover, 1), "at": now.isoformat()}
            else:
                self.avg.setdefault(entity, {})[basis] = {"v": None, "days": 0, "at": now.isoformat()}
        except Exception as err:  # noqa: BLE001 – Statistik ist nur Zusatz
            _LOGGER.debug("Mittelwert %s %s: %s", entity, basis, err)
        finally:
            self._avg_busy.discard((entity, basis))

    def _avg_view(self, entity: str) -> dict:
        s = self._state(entity)
        out = {"entity": entity, "now": _f(s.state) if s else None}
        for b in BASIS_DAYS:
            a = (self.avg.get(entity) or {}).get(b)
            out[b] = a["v"] if a else None
            out[b + "_days"] = a["days"] if a else 0
        return out

    def _avg_entities(self) -> set[str]:
        out = {self.house["outdoor_sensor"]} if self.house["outdoor_sensor"] else set()
        for c in self.circuits.values():
            if self.outdoor_entity(c):
                out.add(self.outdoor_entity(c))
        return out

    # ================================================================ Beobachtung
    def _rewatch(self) -> None:
        w = {SRC_PRESENCE, SRC_ALARM, self.presence()[1], self.alarm()[1]}
        w |= {DAYTIME_START.format(k) for k in PERIODS}
        for c in self.circuits.values():
            w |= set(c.get("devices") or []) | set(self._devices(c)) | set(c.get("windows") or [])
        self._watch = {x for x in w if x}

    @callback
    def _is_watched(self, data: Any) -> bool:
        return data.get("entity_id") in self._watch

    async def _on_state(self, ev: Event) -> None:
        eid = ev.data.get("entity_id")
        if eid.startswith("climate."):
            self._check_manual(eid, ev.data.get("old_state"), ev.data.get("new_state"))
        if eid in (SRC_PRESENCE, SRC_ALARM):
            self._rewatch()
        await self._process()

    def _check_manual(self, eid: str, old, new) -> None:
        if not new or new.state in BAD:
            return
        t_new = _f(new.attributes.get("temperature"))
        t_old = _f(old.attributes.get("temperature")) if old and old.state not in BAD else None
        h_new, h_old = new.state, (old.state if old and old.state not in BAD else None)
        if h_old is None or (t_new == t_old and h_new == h_old):
            return
        d = self.dev.get(eid)
        now = dt_util.utcnow()
        if d:
            if (t_new is None or d.get("t") is None or abs(t_new - d["t"]) < 0.05) and h_new == d.get("hvac", h_new):
                d["ok"] = True
                return
            if not d.get("ok") and (now - _parse(d["at"])).total_seconds() < CONFIRM_S:
                return                           # Zwischenstand während wir auf die Bestätigung warten
        for cid, c in self.circuits.items():
            if not c["enabled"] or c["hold"] == "never" or eid not in self._devices(c):
                continue
            v = self.view.get(cid) or {}
            if v.get("frozen"):                  # Fenster/Heizgrenze/Aus: Hand-Änderung nicht festhalten
                continue
            if c["kind"] == "heat" and t_new is not None and v.get("sent") is not None and abs(t_new - v["sent"]) < 0.05:
                continue
            self.rt.setdefault(cid, {})["manual"] = {
                "t": t_new, "hvac": h_new, "since": now.isoformat(), "key": v.get("key"), "device": eid}
            _LOGGER.info("Heizung %s: Hand-Wert %s (%s) von %s", cid, t_new, h_new, eid)
            self.hass.bus.async_fire(EVENT_CHANGED, {"circuit": cid, "room": c["room"], "action": "manual", "target": t_new})
            self._save()

    async def _tick(self, _now: datetime) -> None:
        for e in self._avg_entities():
            for b in BASIS_DAYS:
                self.hass.async_create_task(self._refresh_avg(e, b))
        await self._process()

    async def _process(self) -> None:
        if self._busy:
            self._again = True
            return
        self._busy = True
        try:
            while True:
                self._again = False
                try:
                    await self._run()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Heizungssteuerung: Fehler im Durchlauf")
                if not self._again:
                    break
        finally:
            self._busy = False
        self._push()

    # ================================================================ Entscheiden
    def _house_facts(self, now: datetime) -> dict:
        pres, pres_e = self.presence()
        alarm, alarm_e = self.alarm()
        if pres is False and not self.away_since:
            self.away_since = now.isoformat()
            self._save()
        elif pres is True and self.away_since:
            self.away_since = None
            self._save()
        away_h = (now - _parse(self.away_since)).total_seconds() / 3600 if self.away_since else 0
        h = self.house
        vac, why = False, ""
        ret = False
        vf, vt = _parse(h["vacation_from"]), _parse(h["vacation_to"])
        if h["vacation_enabled"] and vf and vt:
            pre = vt - timedelta(hours=h["vacation_preheat_h"])
            if vf <= now < pre:
                vac, why = True, "Urlaubsschalter"
            elif pre <= now < vt + timedelta(hours=RETURN_GRACE_H) and pres is not True:
                ret = True
        if not vac and h["vacation_alarm"] and alarm == "armed_vacation":
            vac, why = True, "Alarmanlage Urlaub"
        if not vac and h["vacation_away_h"] > 0 and pres is False and away_h >= h["vacation_away_h"]:
            vac, why = True, f"länger als {h['vacation_away_h']:g} h weg"
        return {"presence": pres, "presence_entity": pres_e, "alarm": alarm, "alarm_entity": alarm_e,
                "away_since": self.away_since, "away_h": round(away_h, 2), "vacation": vac and not ret,
                "vacation_reason": why, "returning": ret, "vacation_to": h["vacation_to"] if ret or vac else None}

    def _status(self, c: dict, f: dict, now: datetime) -> tuple[str, str]:
        """Status des Kreises + Zusatztext."""
        if f["returning"]:
            return "return", "Vorheizen vor Urlaubsende"
        if f["vacation"]:
            return "vacation", f["vacation_reason"]
        vals = []
        if c["use_presence"] and f["presence"] is not None:
            vals.append(f["presence"])
        if c["use_alarm"] and f["alarm"] is not None:
            vals.append(f["alarm"] == "disarmed")
        rt = self.rt.setdefault(c["id"], {})
        home = (any(vals) if c["link"] == "or" else all(vals)) if vals else True
        if home:
            rt.pop("nh", None)
            return "home", ""
        nh = _parse(rt.get("nh"))
        if not nh:
            rt["nh"] = now.isoformat()
            nh = now
        left = self.house["away_delay_min"] * 60 - (now - nh).total_seconds()
        if left > 0:
            return "home", f"abwesend in {int(left // 60) + 1} min"
        return "away", ""

    def _season(self, c: dict, key: str, lim: dict, cool: bool) -> tuple[bool, float | None]:
        rt = self.rt.setdefault(c["id"], {})
        if not lim["enabled"]:
            return True, None
        v = self.outdoor(c, lim["basis"])
        cur = rt.get(key, not cool)
        if v is not None:
            if not cool:
                cur = True if v < lim["on"] else False if v > lim["off"] else cur
            else:
                cur = True if v > lim["on"] else False if v < lim["off"] else cur
        rt[key] = cur
        return cur, v

    def _base(self, c: dict, temps: dict, status: str, now: datetime) -> tuple[float, str, str, datetime | None]:
        """(Temperatur, Schlüssel, Text, gültig bis) aus Status + Tageszeit/Festwert."""
        if status == "vacation":
            return temps["vacation"], "vacation", "Urlaub", None
        if status == "away":
            return temps["away"], "away", "Abwesend", None
        if c["mode"] == "fixed":
            return temps["fixed"], f"{status}|fixed", "Festwert", None
        per, nxt = period_at(self.times(c), now)
        return temps[per], f"{status}|{per}", PERIOD_NAMES[per], nxt

    def _heat_ref(self, c: dict) -> dict | None:
        ref = self.circuits.get(c.get("heat_ref") or "")
        if ref and ref["kind"] == "heat" and ref["room"] == c["room"]:
            return ref
        return next((x for x in self.circuits.values() if x["kind"] == "heat" and x["room"] == c["room"]), None)

    def _decide_cool(self, c: dict, f: dict, now: datetime) -> dict:
        status, note = self._status(c, f, now)
        v: dict = {"status": status, "note": note, "frozen": False}
        if c["mode"] == "off":
            return {**v, "hvac": "off", "target": None, "key": "off", "label": "Aus", "frozen": True}
        win, who = self.window_open(c, now)
        if win:
            return {**v, "hvac": "off", "target": None, "key": "window", "label": "Fenster offen", "window": who, "frozen": True}
        ref = self._heat_ref(c)
        cool_temps = dict(c["temps"])
        if c["follow_comfort"] and ref:
            for k in PERIODS + ["fixed"]:
                cool_temps[k] = ref["temps"][k]
        ct, ckey, clabel, until = self._base(ref if (c["follow_comfort"] and ref) else c, cool_temps, status, now)
        cool_ok, cv = self._season(c, "season_cool", c["cool_limit"], True)
        heat_ok = False
        ht = None
        if c["role"] == "heatpump" and c["heat_by"] in ("heatpump", "both"):
            heat_ok, _ = self._season(c, "season_heat", c["heat_limit"], False)
            ht, hkey, hlabel, huntil = self._base(ref or c, (ref or {"temps": DEFAULT_TEMPS_HEAT})["temps"], status, now)
        if status in ("away", "vacation") and c["away_cool"] == "off":
            cool_ok = False
        room = self.room_temp(c)
        rt = self.rt.setdefault(c["id"], {})
        last = rt.get("last_hvac")
        hy = c["hysteresis"]
        hvac = "off"
        if heat_ok and cool_ok and room is not None:
            if room < ht - hy:
                hvac = "heat"
            elif room > ct + hy:
                hvac = "cool"
            else:
                hvac = last if last in ("heat", "cool") else "off"
        elif heat_ok:
            hvac = "heat"
        elif cool_ok:
            hvac = "cool"
        rt["last_hvac"] = hvac
        if hvac == "heat":
            return {**v, "hvac": "heat", "target": ht, "key": hkey, "label": f"Heizen · {hlabel}", "until": _iso(huntil)}
        if hvac == "cool":
            return {**v, "hvac": "cool", "target": ct, "key": ckey, "label": f"Kühlen · {clabel}", "until": _iso(until)}
        why = "Kühlgrenze" if not cool_ok and c["cool_limit"]["enabled"] and status not in ("away", "vacation") else (
            "Abwesend: aus" if status in ("away", "vacation") and c["away_cool"] == "off" else "nicht nötig")
        return {**v, "hvac": "off", "target": None, "key": f"idle|{ckey}", "label": f"Aus · {why}",
                "outdoor": cv, "frozen": why == "Kühlgrenze"}

    def _decide_heat(self, c: dict, f: dict, now: datetime, cool_views: list[tuple[dict, dict]]) -> dict:
        status, note = self._status(c, f, now)
        frost = c["temps"]["frost"]
        v: dict = {"status": status, "note": note, "frozen": True, "hvac": "heat"}
        if c["mode"] == "off":
            return {**v, "target": frost, "key": "off", "label": "Aus (Frostschutz)"}
        win, who = self.window_open(c, now)
        if win:
            return {**v, "target": frost, "key": "window", "label": "Fenster offen", "window": who}
        ok, ov = self._season(c, "season_heat", c["limit"], False)
        if not ok:
            return {**v, "target": frost, "key": "season", "label": "Heizgrenze: aus", "outdoor": ov}
        for cc, cv in cool_views:
            if cc["room"] != c["room"] or not cc["enabled"]:
                continue
            if cv.get("hvac") == "cool":
                return {**v, "target": frost, "key": "cooling", "label": f"{cc['name']} kühlt"}
            if cc["role"] == "heatpump" and cv.get("hvac") == "heat":
                if cc["heat_by"] == "heatpump":
                    return {**v, "target": frost, "key": "heatpump", "label": f"{cc['name']} heizt"}
                out_now = self.outdoor(cc, "now")
                if cc["heat_by"] == "both" and (out_now is None or out_now >= cc["both_below"]):
                    return {**v, "target": frost, "key": "heatpump", "label": f"{cc['name']} heizt"}
        t, key, label, until = self._base(c, c["temps"], status, now)
        return {**v, "frozen": False, "target": max(t, frost), "key": key, "label": label, "until": _iso(until), "outdoor": ov}

    def _apply_manual(self, c: dict, v: dict, now: datetime) -> dict:
        rt = self.rt.setdefault(c["id"], {})
        m = rt.get("manual")
        if not m:
            return v
        since = _parse(m["since"]) or now
        cap = since + timedelta(hours=c["hold_hours"])
        until: datetime | None = cap
        if v.get("frozen"):
            valid = False                       # Fenster/Heizgrenze/Aus beenden den Hand-Wert
        elif c["hold"] == "hours":
            valid = now < cap
        else:
            # bis zur nächsten Tageszeit bzw. Statuswechsel; ohne Tageszeit (Festwert, abwesend) höchstens hold_hours
            valid = m.get("key") == v.get("key")
            if v.get("until"):
                until = _parse(v["until"])
            else:
                valid = valid and now < cap
        if not valid:
            rt.pop("manual", None)
            self._save()
            return v
        return {**v, "manual": True, "target": m["t"], "hvac": m.get("hvac") or v.get("hvac"),
                "label": f"Hand · {v.get('label')}", "until": _iso(until)}

    # ================================================================ Durchlauf
    async def _run(self) -> None:
        now = dt_util.now()
        f = self._house_facts(now)
        self.facts = f
        cools = [c for c in self.circuits.values() if c["kind"] == "cool"]
        heats = [c for c in self.circuits.values() if c["kind"] == "heat"]
        cool_views: list[tuple[dict, dict]] = []
        for c in cools + heats:
            try:
                if c["kind"] == "cool":
                    v = self._decide_cool(c, f, now)
                    cool_views.append((c, v))
                else:
                    v = self._decide_heat(c, f, now, cool_views)
                v = self._apply_manual(c, v, now)
                v["room_temp"] = self.room_temp(c)
                v["devices"] = self._devices(c)
                prev = self.view.get(c["id"]) or {}
                if c["enabled"] and not v.get("manual"):
                    v["sent"] = await self._send_circuit(c, v, now)
                else:
                    v["sent"] = prev.get("sent")
                if c["enabled"] and (prev.get("target"), prev.get("hvac"), prev.get("key")) != (v.get("target"), v.get("hvac"), v.get("key")):
                    self.hass.bus.async_fire(EVENT_CHANGED, {"circuit": c["id"], "room": c["room"], "action": "target",
                                                             "target": v.get("target"), "hvac": v.get("hvac"), "label": v.get("label")})
                self.view[c["id"]] = v
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Heizungssteuerung: Kreis %s", c.get("id"))

    def _compensation(self, c: dict, v: dict, now: datetime) -> float:
        if c["kind"] != "heat" or not c["compensate"] or v.get("frozen") or not c.get("sensor_in"):
            return 0.0
        rt = self.rt.setdefault(c["id"], {})
        comp = rt.get("comp")
        if comp and (now - _parse(comp["at"])).total_seconds() < COMP_MIN_GAP_S:
            return comp["o"]
        room = _f(self._txt(c["sensor_in"]))
        devs = [_f((self._state(e).attributes if self._state(e) else {}).get("current_temperature")) for e in self._devices(c)]
        devs = [x for x in devs if x is not None]
        if room is None or not devs:
            return comp["o"] if comp else 0.0
        o = max(-3.0, min(3.0, round((sum(devs) / len(devs) - room) * 2) / 2))
        if comp and abs(o - comp["o"]) < 0.5:
            o = comp["o"]
        rt["comp"] = {"o": o, "at": now.isoformat()}
        return o

    async def _send_circuit(self, c: dict, v: dict, now: datetime) -> float | None:
        target = v.get("target")
        hvac = v.get("hvac")
        sent = None
        off = self._compensation(c, v, now)
        if target is not None:
            target = target + off
            v["offset"] = off
        for e in self._devices(c):
            s = self._state(e)
            if not s or s.state in BAD:
                continue
            step = _f(s.attributes.get("target_temp_step")) or 0.5
            lo, hi = _f(s.attributes.get("min_temp")) or 4.5, _f(s.attributes.get("max_temp")) or 30.5
            t = None if target is None else max(lo, min(hi, round(target / step) * step))
            sent = t if t is not None else sent
            modes = s.attributes.get("hvac_modes") or []
            want_hvac = hvac
            if c["kind"] == "heat":
                if s.state == "off":
                    continue                    # Thermostat von Hand aus → nicht anfassen
                want_hvac = "heat" if s.state != "heat" and "heat" in modes else s.state
            cur_t = _f(s.attributes.get("temperature"))
            need = (want_hvac != s.state) or (t is not None and (cur_t is None or abs(cur_t - t) >= 0.05))
            if not need:
                d = self.dev.get(e)
                if d and not d.get("ok"):
                    d["ok"] = True
                continue
            await self._send(e, t, want_hvac, s.state, now)
        return sent

    async def _send(self, e: str, t: float | None, hvac: str, cur_hvac: str, now: datetime) -> None:
        d = self.dev.get(e)
        if d:
            age = (now - _parse(d["at"])).total_seconds()
            same = d.get("t") == t and d.get("hvac") == hvac
            if age < SEND_MIN_GAP_S:
                return
            if same and not d.get("ok") and age < CONFIRM_S:
                return
            if same and d.get("tries", 0) >= MAX_TRIES and age < RETRY_LATE_S:
                return
            tries = d.get("tries", 0) + 1 if same else 1
        else:
            tries = 1
        self.dev[e] = {"t": t, "hvac": hvac, "at": now.isoformat(), "tries": tries, "ok": False}
        ctx = Context(parent_id=self.root.id)
        try:
            if hvac == "off":
                await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": e, "hvac_mode": "off"},
                                                    blocking=False, context=ctx)
            else:
                data: dict = {"entity_id": e, "temperature": t}
                if hvac != cur_hvac:
                    data["hvac_mode"] = hvac
                await self.hass.services.async_call("climate", "set_temperature", data, blocking=False, context=ctx)
            _LOGGER.debug("Heizung → %s: %s %s (Versuch %s)", e, hvac, t, tries)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Heizung → %s fehlgeschlagen: %s", e, err)
        self._save()

    # ================================================================ Anzeige
    def circuit_view(self, cid: str) -> dict:
        c = self.circuits[cid]
        v = self.view.get(cid) or {}
        rt = self.rt.get(cid) or {}
        out_e = self.outdoor_entity(c)
        devs = []
        for e in v.get("devices") or self._devices(c):
            s = self._state(e)
            d = self.dev.get(e) or {}
            devs.append({"entity_id": e, "state": s.state if s else None,
                         "target": _f(s.attributes.get("temperature")) if s else None,
                         "current": _f(s.attributes.get("current_temperature")) if s else None,
                         "pending": bool(d) and not d.get("ok"), "tries": d.get("tries", 0)})
        return {**c,
                "times_used": self.times(c),
                "status": v.get("status"), "status_name": STATUS_NAMES.get(v.get("status"), ""), "note": v.get("note"),
                "target": v.get("target"), "hvac": v.get("hvac"), "label": v.get("label"), "until": v.get("until"),
                "key": v.get("key"), "manual": v.get("manual", False), "window": v.get("window"),
                "offset": v.get("offset", 0.0), "sent": v.get("sent"), "room_temp": v.get("room_temp"),
                "outdoor_entity": out_e, "season_heat": rt.get("season_heat"), "season_cool": rt.get("season_cool"),
                "device_states": devs}

    def room_view(self, room: str) -> dict:
        cs = [self.circuit_view(k) for k, c in sorted(self.circuits.items(), key=lambda x: (x[1]["kind"], x[1]["name"].lower()))
              if c["room"] == room]
        return {"circuits": cs, "version": VERSION}

    def house_view(self) -> dict:
        return {"settings": self.house, "facts": self.facts,
                "outdoor": {e: self._avg_view(e) for e in self._avg_entities()}, "version": VERSION}

    def room_state(self, room: str) -> float | None:
        for c in sorted(self.circuits.values(), key=lambda x: x["name"].lower()):
            if c["room"] == room and c["kind"] == "heat" and c["enabled"]:
                return (self.view.get(c["id"]) or {}).get("target")
        return None

    @callback
    def _push(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
