"""Konstanten der Heizungssteuerung (heat_control)."""

DOMAIN = "heat_control"
VERSION = "1.0.0"

CONF_ROOMS = "rooms"
DEFAULT_ROOMS = ["wz", "bu", "ku", "sz", "fl", "ke", "ba", "bz"]

STORE_KEY = "heat_control"
STORE_VERSION = 1

TICK_SECONDS = [5]               # Prüftakt einmal pro Minute; zusätzlich sofort bei Änderung einer Quelle
SEND_MIN_GAP_S = 60              # Funk schonen: höchstens ein Befehl je Gerät pro Minute
CONFIRM_S = 180                  # so lange warten wir, bis ein Thermostat den neuen Sollwert meldet
MAX_TRIES = 3                    # danach nur noch alle 15 min ein neuer Versuch
RETRY_LATE_S = 900
COMP_MIN_GAP_S = 900             # Raumsensor-Ausgleich höchstens alle 15 min neu berechnen
AVG_REFRESH_S = {"1d": 600, "7d": 3600, "30d": 3600, "365d": 6 * 3600}
MAX_CIRCUITS_PER_ROOM = 8
RETURN_GRACE_H = 6               # nach Urlaubsende so lange "Rückkehr" (Komfort), falls noch niemand da ist

SIGNAL_UPDATE = "heat_control_update"
EVENT_CHANGED = "heat_control_changed"

# Hausweite Quellen – dieselben Helfer wie Licht/Anwesenheitssimulation
SRC_ALARM = "input_text.haus_light_motion_cfg_alarm_panel"
SRC_PRESENCE = "input_text.haus_light_motion_cfg_presence_sensor"
SRC_SIM = "binary_sensor.haus_sys_presence_sim_active"
DAYTIME_START = "input_datetime.haus_sys_daytime_cfg_{}_start"
HEAT_SENSORS_HELPER = "input_text.heizung_sensoren_{room}"   # Raum-Panel: current=…;valve=…

PERIODS = ["morning", "midday", "afternoon", "evening", "night"]
PERIOD_NAMES = {"morning": "Morgens", "midday": "Mittags", "afternoon": "Nachmittags", "evening": "Abends", "night": "Nachts"}
DEFAULT_TIMES = {"morning": "06:00", "midday": "11:00", "afternoon": "14:00", "evening": "18:00", "night": "22:00"}

KINDS = ["heat", "cool"]
MODES = ["schedule", "fixed", "off"]
BASES = ["now", "1d", "7d", "30d", "365d"]
BASIS_DAYS = {"1d": 1, "7d": 7, "30d": 30, "365d": 365}
HOLD = ["period", "hours", "never"]
ROLES = ["cool", "heatpump"]
HEAT_BY = ["thermostats", "heatpump", "both"]
LINKS = ["or", "and"]
AWAY_COOL = ["temp", "off"]

DEFAULT_HOUSE = {
    "outdoor_sensor": "",          # z. B. sensor.outdoor_temperature (leer = keine Heizgrenze nach Außentemperatur)
    "away_delay_min": 15,          # erst so lange niemand da → Abwesend
    "vacation_enabled": False,     # Urlaubsschalter
    "vacation_from": "",           # JJJJ-MM-TTThh:mm (lokal)
    "vacation_to": "",
    "vacation_preheat_h": 3,       # so viele Stunden vor Urlaubsende wieder Komfort
    "vacation_alarm": True,        # Alarm armed_vacation = Urlaub
    "vacation_away_h": 24,         # länger als … h abwesend = Urlaub (0 = aus)
}

DEFAULT_TEMPS_HEAT = {"night": 18.0, "morning": 21.0, "midday": 20.0, "afternoon": 20.0, "evening": 21.0,
                      "fixed": 20.0, "away": 17.0, "vacation": 15.0, "frost": 7.0}
DEFAULT_TEMPS_COOL = {"night": 24.0, "morning": 25.0, "midday": 24.0, "afternoon": 24.0, "evening": 24.0,
                      "fixed": 24.0, "away": 28.0, "vacation": 30.0, "frost": 7.0}

DEFAULT_CIRCUIT = {
    "name": "Heizkörper",
    "kind": "heat",
    "enabled": False,
    "devices": [],
    "mode": "schedule",
    "temps": DEFAULT_TEMPS_HEAT,
    "own_times": False,
    "times": DEFAULT_TIMES,
    "use_presence": True,
    "use_alarm": True,
    "link": "or",                  # anwesend ODER/UND Alarm aus = zuhause
    "sensor_in": "",               # leer = Gerät selbst
    "sensor_out": "",              # leer = hausweiter Außensensor
    "compensate": False,           # Sollwert um (Thermostat − Raumsensor) korrigieren
    "limit": {"enabled": False, "basis": "1d", "on": 15.0, "off": 17.0},   # heat: an unter on, aus über off
    "windows": [],
    "window_delay_s": 30,
    "window_tilted": True,         # gekippt zählt als offen
    "hold": "period",
    "hold_hours": 2,
    # nur Kühlkreis
    "role": "cool",
    "heat_by": "thermostats",
    "both_below": 5.0,             # bei heat_by = both: Thermostate helfen unter … °C außen
    "hysteresis": 0.5,
    "heat_ref": "",                # Heizkreis, dessen Temperaturen die Wärmepumpe beim Heizen nutzt ("" = erster im Raum)
    "follow_comfort": False,       # Kühlen: Komfortwerte (Tageszeiten/Festwert) vom Heizkreis übernehmen
    "away_cool": "temp",
    "cool_limit": {"enabled": True, "basis": "now", "on": 22.0, "off": 20.0},  # cool: an über on, aus unter off
    "heat_limit": {"enabled": True, "basis": "1d", "on": 15.0, "off": 17.0},   # Wärmepumpe heizt
}
