"""
<plugin key="Joulo" name="Joulo (EV charging ERE yield)" author="Domoticz" version="1.0.0" wikilink="https://developer.joulo.nl/" externallink="https://joulo.nl/">
    <description>
        <h2>Joulo</h2><br/>
        Brings your Joulo EV charger data into Domoticz: live charger status, charged energy, ERE credits and
        ERE yield (with full history), the ERE position of your account and a ready-made page at
        <b>#/Custom/Joulo</b> with day, week, month and year overviews.<br/><br/>
        Create an API token in the Joulo dashboard (Developer, API) and paste it below. Everything else is
        guided in the Joulo page.
    </description>
    <params>
        <param field="ApiToken" label="API token" password="true" width="320px" required="true" default=""/>
        <param field="ImportHistory" type="boolean" label="Import history on first start" default="true">
            <description>
                Imports all your past Joulo sessions into the energy, ERE credits and ERE yield devices. Existing days are overwritten, so it is safe to repeat (use the page to import again).
            </description>
        </param>
        <param field="RealKwhIdx" type="device" filter="kwh" label="Real charger kWh device" width="320px">
            <description>
                Optional. The kWh meter that measures your real charger. The Joulo page uses its cost next to the ERE yield. You can also pick it in the Joulo page.
            </description>
        </param>
        <param field="AllowControl" type="boolean" label="Allow control" default="false">
            <description>
                Turn on to reveal the control settings below.
            </description>
        </param>
        <group label="Control">
            <param field="AllowRestart" type="boolean" label="Allow restart" default="false" visible_when="AllowControl=true">
                <description>
                    Adds a restart button per charger (device and page). Joulo only supports restarting OCPP chargers.
                </description>
            </param>
        </group>
        <group label="Advanced">
            <param field="ChargerPoll" type="number" label="Charger poll (s)" min="30" max="3600" step="10" default="60" width="100px">
                <description>
                    How often the charger status is read: online or offline, charging, power and the running session. Lower is more live but uses more of the Joulo rate limit.
                </description>
            </param>
            <param field="SessionPollActive" type="number" label="Session poll while charging (s)" min="30" max="3600" step="10" default="60" width="100px">
                <description>
                    How often the session list is refreshed while a car is charging, so the day totals and the history follow the running session.
                </description>
            </param>
            <param field="SessionPollIdle" type="number" label="Session poll while idle (s)" min="300" max="86400" step="60" default="3600" width="100px">
                <description>
                    How often the session list is refreshed when nothing is charging. New or corrected sessions are still picked up, just later.
                </description>
            </param>
            <param field="EnergyPoll" type="number" label="Account and ERE poll (s)" min="600" max="86400" step="60" default="3600" width="100px">
                <description>
                    How often the account totals and the ERE position (paid, payable, reserved, forecast, blocked reasons) are read. Joulo advises about once per hour, this data changes slowly.
                </description>
            </param>
            <param field="Debug" type="boolean" label="Debug logging" default="false">
                <description>
                    Logs every API call (never the token) and every history write. Only needed when something does not work, the hardware log level must also include status messages.
                </description>
            </param>
        </group>
    </params>
</plugin>
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import DomoticzEx as Domoticz

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import joulo_api  # noqa: E402
import joulo_logic  # noqa: E402

VERSION = "1.0.0"
PAGE_NAME = "Joulo.html"

# Units on a charger device
C_ENERGY, C_EURO, C_ERE, C_POWER, C_STATUS, C_CHARGING, C_SESSION, C_METER, C_ALERT, C_RESTART = range(1, 11)
# Units on the account device
A_KWH, A_ERE, A_SESSIONS, A_PAID, A_PAYABLE, A_RESERVED, A_UNSOLD, A_EXPECTED, A_PRICE, A_QUARTER, A_ALERT_ERE, A_ALERT_API = range(1, 13)

ACCOUNT_ID = "JOULO_ACCOUNT"
COUNTER_TYPE, COUNTER_SUBTYPE = 243, 33  # pTypeGeneral, sTypeManagedCounter
WRITES_PER_BEAT = 150
SESSION_CAP = 3000  # sessions sent to the page


def _int(name, default):
    try:
        return int(float(Parameters.get(name, default)))
    except (TypeError, ValueError):
        return default


def _bool(name):
    return str(Parameters.get(name, "false")).strip().lower() in ("true", "1", "yes")


def charger_device_id(charger_id):
    return "JC" + charger_id.replace("-", "")[:20]


class BasePlugin:
    def __init__(self):
        self.client = None
        self.thread = None
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.stopping = False
        self.inflight = set()
        self.next_run = {}
        self.backoff_until = 0
        self.auth_failed = False
        self.state = {}
        self.dirty = False
        self.state_path = ""
        self.chargers = []
        self.energy = None
        self.ere = None
        self.api_error = ""
        self.last_ok = 0
        self.pending = deque()
        self.import_running = False
        self.import_progress = 0
        self.import_wanted = False
        self.snapshot_due = False
        self.state_needs_history = False
        self.connected_alert = False
        self.debug = False
        self.hwid = "0"

    # ------------------------------------------------------------------ logging
    def log(self, text):
        Domoticz.Status(text)

    def dbg(self, text):
        if self.debug:
            Domoticz.Status("[debug] " + text)

    # ------------------------------------------------------------------ lifecycle
    def onStart(self):
        self.debug = _bool("Debug")
        self.hwid = str(Parameters.get("HardwareID", "0"))
        Domoticz.Heartbeat(10)
        token = Parameters.get("ApiToken", "")
        if not token:
            Domoticz.Error("Joulo: no API token set, edit the hardware and paste the token from the Joulo dashboard (Developer, API)")
        self.client = joulo_api.JouloClient(token)
        self._load_state()
        self._install_page()
        self._ensure_account_devices()
        for charger_id in list(self.state.get("chargers", {})):
            self._ensure_charger_devices(self.state["chargers"][charger_id])
        self.import_wanted = (not self.state.get("import_done")) and _bool("ImportHistory")
        self.thread = threading.Thread(target=self._worker, name="JouloWorker", daemon=True)
        self.thread.start()
        self.log("Joulo %s started (hardware %s)" % (VERSION, self.hwid))

    def onStop(self):
        self.stopping = True
        self.jobs.put(None)
        if self.thread:
            self.thread.join(5)
        self._save_state(force=True)
        self._remove_page()
        self.log("Joulo stopped")

    def onHeartbeat(self):
        self._drain_results()
        if self.state_needs_history and self.state.get("import_done"):
            self.state_needs_history = False
            self._queue_history()
        self._schedule()
        self._process_writes()
        if self.dirty:
            self._save_state()
        if self.snapshot_due:
            self.snapshot_due = False
            self._push_snapshot()

    # ------------------------------------------------------------------ worker thread (HTTP only, no Domoticz calls)
    def _worker(self):
        while not self.stopping:
            job = self.jobs.get()
            if job is None:
                break
            name, args = job
            try:
                result = self._run_job(name, args)
                self.results.put((name, args, result, None))
            except Exception as err:  # noqa: BLE001
                self.results.put((name, args, None, err))

    def _run_job(self, name, args):
        c = self.client
        if name == "chargers":
            return c.get_chargers()
        if name == "sessions":
            if args.get("full"):
                return c.iter_sessions(on_page=lambda n: self.results.put(("import_progress", {}, n, None)))
            return c.iter_sessions(from_=joulo_api.iso_now_minus(args.get("days", 7)))
        if name == "energy":
            return c.get_energy()
        if name == "ere":
            return c.get_ere_position()
        if name == "estimate":
            return c.get_sessions(limit=1).get("estimate_basis")
        if name == "restart":
            return c.restart_charger(args["charger_id"], args.get("hard", False))
        raise ValueError("unknown job " + name)

    def _enqueue(self, name, **args):
        key = name
        if key in self.inflight:
            return False
        self.inflight.add(key)
        self.jobs.put((name, args))
        return True

    # ------------------------------------------------------------------ scheduling
    def _any_charging(self):
        return any(c.get("is_charging") for c in self.chargers)

    def _schedule(self):
        if self.auth_failed or not self.client:
            return
        now = time.time()
        if now < self.backoff_until:
            return
        due = self.next_run

        if now >= due.get("chargers", 0) and self._enqueue("chargers"):
            due["chargers"] = now + _int("ChargerPoll", 60)

        if now >= due.get("sessions", 0):
            full = self.import_wanted and not self.state.get("import_done")
            if self._enqueue("sessions", full=full):
                if full:
                    self.import_running = True
                    self.import_progress = 0
                    self.import_wanted = False
                    self.snapshot_due = True
                interval = _int("SessionPollActive", 60) if self._any_charging() else _int("SessionPollIdle", 3600)
                due["sessions"] = now + interval

        if now >= due.get("energy", 0):
            if self._enqueue("energy"):
                self._enqueue("ere")
                due["energy"] = now + _int("EnergyPoll", 3600)

    # ------------------------------------------------------------------ results (main thread)
    def _drain_results(self):
        while True:
            try:
                name, args, result, err = self.results.get_nowait()
            except queue.Empty:
                return
            if name == "import_progress":
                self.import_progress = result
                self._send({"type": "import_progress", "sessions": result})
                continue
            self.inflight.discard(name)
            if err is not None:
                self._handle_error(name, args, err)
                continue
            self._mark_ok()
            try:
                getattr(self, "_on_" + name)(args, result)
            except Exception as exc:  # noqa: BLE001
                Domoticz.Error("Joulo: failed to process %s: %r" % (name, exc))

    def _mark_ok(self):
        self.last_ok = time.time()
        if self.api_error or not self.connected_alert:
            self.api_error = ""
            self.connected_alert = True
            self._set_alert(ACCOUNT_ID, A_ALERT_API, 0, "Connected to Joulo")
            self.snapshot_due = True

    def _handle_error(self, name, args, err):
        if name == "sessions" and args.get("full"):
            self.import_running = False
            self.import_wanted = False
        text = str(err)
        status = getattr(err, "status", 0)
        self.connected_alert = False
        if getattr(err, "is_auth", False):
            if self.auth_failed:
                return  # already reported, the other queued requests fail the same way
            self.auth_failed = True
            self.api_error = "Token rejected by Joulo (%s). Create a new token in the Joulo dashboard and edit the hardware." % text
            level = 4
        elif getattr(err, "is_rate_limited", False):
            self.backoff_until = time.time() + max(getattr(err, "retry_after", 0), 300)
            self.api_error = "Joulo rate limit reached, backing off"
            level = 2
        elif name == "restart":
            Domoticz.Error("Joulo: restart failed: %s" % text)
            self._send({"type": "restart_result", "ok": False, "error": text})
            return
        else:
            self.api_error = "Joulo is unreachable or returned an error (%s)" % text
            level = 1
        Domoticz.Error("Joulo: %s failed: %s" % (name, text))
        self._set_alert(ACCOUNT_ID, A_ALERT_API, level, self.api_error)
        self.snapshot_due = True
        self.dbg("status=%s" % status)

    def _on_chargers(self, args, chargers):
        self.chargers = chargers
        known = self.state.setdefault("chargers", {})
        for c in chargers:
            known[c["id"]] = {"id": c["id"], "nickname": c.get("nickname") or c["id"][:8], "brand": c.get("brand"), "model": c.get("model")}
            self._ensure_charger_devices(known[c["id"]])
            self._update_live(c)
        self.dirty = True
        self.snapshot_due = True

    def _on_sessions(self, args, sessions):
        cache = self.state.setdefault("sessions", {})
        changed = joulo_logic.merge_sessions(cache, sessions)
        self.dbg("sessions fetched=%d changed days=%d" % (len(sessions), len(changed)))
        if args.get("full"):
            self.state["import_done"] = True
            self.import_running = False
            self.log("Joulo: imported %d sessions" % len(sessions))
            changed = {"all"}
        if self.state.get("import_done") and changed:
            self._queue_history()
        self.dirty = True
        self.snapshot_due = True
        if not self.state.get("estimate"):
            self._enqueue("estimate")

    def _on_estimate(self, args, basis):
        if basis:
            self.state["estimate"] = basis
            self.dirty = True
            self.snapshot_due = True

    def _on_energy(self, args, energy):
        self.energy = energy
        self._update_value(ACCOUNT_ID, A_KWH, "%.1f" % energy.get("total_kwh_all", energy.get("total_kwh", 0)))
        self._update_value(ACCOUNT_ID, A_ERE, "%.1f" % energy.get("total_ere_credits", 0))
        self._update_value(ACCOUNT_ID, A_SESSIONS, "%d" % energy.get("total_sessions_all", energy.get("total_sessions", 0)))
        self.snapshot_due = True

    def _on_ere(self, args, ere):
        self.ere = ere
        self._update_value(ACCOUNT_ID, A_PAID, "%.2f" % ere.get("paid", {}).get("net_eur", 0))
        self._update_value(ACCOUNT_ID, A_PAYABLE, "%.2f" % ere.get("payable", {}).get("net_eur", 0))
        self._update_value(ACCOUNT_ID, A_RESERVED, "%.2f" % ere.get("reserved", {}).get("net_eur", 0))
        self._update_value(ACCOUNT_ID, A_UNSOLD, "%.2f" % ere.get("unsold", {}).get("forecast_net_eur", 0))
        self._update_value(ACCOUNT_ID, A_EXPECTED, "%.2f" % ere.get("total_expected_eur", 0))
        self._update_value(ACCOUNT_ID, A_PRICE, "%.3f" % ere.get("indicative_price_per_ere", 0))
        quarters = ere.get("quarters") or []
        if quarters:
            self._update_value(ACCOUNT_ID, A_QUARTER, "%.1f" % quarters[-1].get("ere", 0))
        blocked = ere.get("blocked") or {}
        reason = blocked.get("reason")
        if reason:
            self._set_alert(ACCOUNT_ID, A_ALERT_ERE, 2, "ERE blocked: %s (EUR %.2f)" % (reason, blocked.get("forecast_net_eur", 0)))
        else:
            self._set_alert(ACCOUNT_ID, A_ALERT_ERE, 0, "ERE flowing")
        self.snapshot_due = True

    def _on_restart(self, args, result):
        status, payload = result
        self.log("Joulo: restart of charger %s answered %s" % (args.get("charger_id", "")[:8], status))
        self._send({"type": "restart_result", "ok": status in (200, 202), "status": payload.get("status"), "error": payload.get("error")})

    # ------------------------------------------------------------------ devices
    def _unit(self, device_id, unit):
        if device_id not in Devices:
            return None
        units = Devices[device_id].Units
        return units[unit] if unit in units else None

    def _create(self, device_id, unit, name, **kw):
        if self._unit(device_id, unit) is not None:
            return False
        u = Domoticz.Unit(Name=name, DeviceID=device_id, Unit=unit, Used=1, **{k: v for k, v in kw.items() if k not in ("divider",)})
        if kw.get("divider"):
            u.Adjustment2 = float(kw["divider"])
        u.Create()
        # a (re)created counter has no history yet, so forget what was written before
        self.state.get("written", {}).pop("%s:%d" % (device_id, unit), None)
        self.state_needs_history = True
        return True

    def _counter(self, device_id, unit, name, switchtype, divider, quantity=None):
        options = {}
        if quantity:
            options = {"ValueQuantity": quantity, "ValueUnits": quantity}
        self._create(device_id, unit, name, Type=COUNTER_TYPE, Subtype=COUNTER_SUBTYPE, Switchtype=switchtype,
                     Options=options, divider=divider)

    def _custom(self, device_id, unit, name, axis):
        self._create(device_id, unit, name, Type=243, Subtype=31, Options={"Custom": "1;" + axis})

    def _ensure_account_devices(self):
        d = ACCOUNT_ID
        self._custom(d, A_KWH, "Joulo total energy", "kWh")
        self._custom(d, A_ERE, "Joulo total ERE credits", "ERE")
        self._custom(d, A_SESSIONS, "Joulo total sessions", "sessions")
        self._custom(d, A_PAID, "Joulo ERE paid", "EUR")
        self._custom(d, A_PAYABLE, "Joulo ERE payable", "EUR")
        self._custom(d, A_RESERVED, "Joulo ERE reserved", "EUR")
        self._custom(d, A_UNSOLD, "Joulo ERE unsold forecast", "EUR")
        self._custom(d, A_EXPECTED, "Joulo ERE expected this year", "EUR")
        self._custom(d, A_PRICE, "Joulo ERE price", "EUR/ERE")
        self._custom(d, A_QUARTER, "Joulo ERE current quarter", "ERE")
        self._create(d, A_ALERT_ERE, "Joulo ERE status", Type=243, Subtype=22)
        self._create(d, A_ALERT_API, "Joulo connection", Type=243, Subtype=22)

    def _ensure_charger_devices(self, charger):
        d = charger_device_id(charger["id"])
        n = charger["nickname"]
        self._counter(d, C_ENERGY, n + " energy", 0, 1000)
        self._counter(d, C_EURO, n + " ERE yield", 3, 100, "EUR")
        self._counter(d, C_ERE, n + " ERE credits", 3, 1000, "ERE")
        self._custom(d, C_POWER, n + " power", "kW")
        self._create(d, C_STATUS, n + " status", Type=243, Subtype=19)
        self._create(d, C_CHARGING, n + " charging", Type=244, Subtype=73, Switchtype=0)
        self._custom(d, C_SESSION, n + " session energy", "kWh")
        self._custom(d, C_METER, n + " meter reading", "kWh")
        self._create(d, C_ALERT, n + " connection", Type=243, Subtype=22)
        if _bool("AllowControl") and _bool("AllowRestart"):
            self._create(d, C_RESTART, n + " restart", Type=244, Subtype=73, Switchtype=9)

    def _write_unit(self, device_id, unit, nvalue, svalue):
        u = self._unit(device_id, unit)
        if u is None:
            return
        u.nValue = nvalue
        u.sValue = svalue
        u.Update(Log=False)

    def _update_value(self, device_id, unit, svalue):
        self._write_unit(device_id, unit, 0, svalue)

    def _set_alert(self, device_id, unit, level, text):
        self._write_unit(device_id, unit, level, text)

    def _update_live(self, c):
        d = charger_device_id(c["id"])
        power = c.get("power_kw")
        self._update_value(d, C_POWER, "%.2f" % (power or 0))
        charging = bool(c.get("is_charging"))
        state = "Charging" if charging else (c.get("connection_status") or "unknown").capitalize()
        self._update_value(d, C_STATUS, state)
        self._write_unit(d, C_CHARGING, 1 if charging else 0, "On" if charging else "Off")
        cs = c.get("current_session") or {}
        self._update_value(d, C_SESSION, "%.2f" % (cs.get("kwh_so_far") or 0))
        if c.get("latest_meter_wh") is not None:
            self._update_value(d, C_METER, "%.1f" % (c["latest_meter_wh"] / 1000.0))
        online = (c.get("connection_status") or "").lower() == "online"
        self._set_alert(d, C_ALERT, 0 if online else 3, "Online" if online else "Charger %s" % (c.get("connection_status") or "offline"))

    # ------------------------------------------------------------------ history writes
    def _queue_history(self):
        sessions = list(self.state.get("sessions", {}).values())
        written = self.state.setdefault("written", {})
        chargers = set(self.state.get("chargers", {}))
        chargers.update(s.get("charger_id") for s in sessions if s.get("charger_id"))
        today = datetime.now().strftime("%Y-%m-%d")
        queued = 0
        self.state_needs_history = False
        self.pending.clear()  # the diff below is computed from the full session set, so it replaces anything queued
        for charger_id in chargers:
            if charger_id not in self.state.get("chargers", {}):
                continue
            d = charger_device_id(charger_id)
            days = joulo_logic.aggregate_days(sessions, charger_id)
            for field, unit in (("wh", C_ENERGY), ("cents", C_EURO), ("ere_milli", C_ERE)):
                key = "%s:%d" % (d, unit)
                done = written.setdefault(key, {})
                rows = joulo_logic.cumulative(days, field)
                for day, total, usage in rows:
                    if done.get(day) != [total, usage]:
                        self.pending.append((d, unit, key, day, total, usage))
                        queued += 1
                # days we wrote before that no longer have sessions (a session moved or was removed): zero them
                known = {day for day, _, _ in rows}
                for day in sorted(set(done) - known):
                    carried = max((t for dd, t, _ in rows if dd < day), default=0)
                    if done[day] != [carried, 0]:
                        self.pending.append((d, unit, key, day, carried, 0))
                        queued += 1
                # the dashboard card: lifetime counter and today's usage
                last_total = rows[-1][1] if rows else 0
                today_usage = days.get(today, {}).get(field, 0)
                self.pending.append((d, unit, key, None, last_total, today_usage))
        self.dbg("history writes queued: %d" % queued)
        if queued:
            self.log("Joulo: writing %d history rows" % queued)
        self.snapshot_due = True

    def _process_writes(self):
        count = 0
        written = self.state.setdefault("written", {})
        while self.pending and count < WRITES_PER_BEAT:
            d, unit, key, day, total, usage = self.pending.popleft()
            if day is None:
                self._write_unit(d, unit, 0, "%d;%d" % (total, usage))
            else:
                self._write_unit(d, unit, 0, "%d;%d;%s" % (total, usage, day))
                written.setdefault(key, {})[day] = [total, usage]
            count += 1
        if count:
            self.dirty = True
            if not self.pending:
                self.log("Joulo: history written")
                self.snapshot_due = True

    # ------------------------------------------------------------------ state file
    def _data_dir(self):
        base = Parameters.get("UserDataFolder") or Parameters.get("StartupFolder") or Parameters.get("HomeFolder") or _HERE
        path = os.path.join(base, "joulo-data")
        os.makedirs(path, exist_ok=True)
        return path

    def _load_state(self):
        try:
            self.state_path = os.path.join(self._data_dir(), "joulo_%s.json" % self.hwid)
        except OSError as exc:
            Domoticz.Error("Joulo: cannot create data folder: %r" % exc)
            self.state_path = ""
        self.state = {}
        if self.state_path and os.path.isfile(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as fh:
                    self.state = json.load(fh)
            except (OSError, ValueError) as exc:
                Domoticz.Error("Joulo: state file unreadable, starting fresh: %r" % exc)

    def _save_state(self, force=False):
        if not self.state_path or (not self.dirty and not force):
            return
        try:
            fd, tmp = tempfile.mkstemp(prefix=".joulo-", dir=os.path.dirname(self.state_path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.state_path)
            self.dirty = False
        except OSError as exc:
            Domoticz.Error("Joulo: cannot save state: %r" % exc)

    # ------------------------------------------------------------------ custom page
    def _templates_dir(self):
        return os.path.join(os.path.abspath(os.path.join(_HERE, "..", "..")), "www", "templates")

    def _install_page(self):
        src = os.path.join(_HERE, PAGE_NAME)
        dest = os.path.join(self._templates_dir(), PAGE_NAME)
        if not os.path.isfile(src):
            Domoticz.Error("Joulo: page template %s missing" % src)
            return
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            self.log("Joulo: page installed, open it at #/Custom/Joulo")
        except OSError as exc:
            Domoticz.Error("Joulo: cannot install page: %r" % exc)

    def _remove_page(self):
        dest = os.path.join(self._templates_dir(), PAGE_NAME)
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError as exc:
            Domoticz.Error("Joulo: cannot remove page: %r" % exc)

    # ------------------------------------------------------------------ web socket channel
    @staticmethod
    def _without_none(value):
        # Older Domoticz versions turn a Python None into the text "None" in the JSON sent to the page
        if isinstance(value, dict):
            return {k: BasePlugin._without_none(v) for k, v in value.items() if v is not None}
        if isinstance(value, (list, tuple)):
            return [BasePlugin._without_none(v) for v in value if v is not None]
        return value

    def _send(self, payload):
        payload = self._without_none(payload)
        payload.setdefault("hwid", self.hwid)
        try:
            Domoticz.WebSocketSend(payload)
        except Exception as exc:  # noqa: BLE001
            self.dbg("websocket send failed: %r" % exc)

    def real_idx(self, charger_id):
        stored = (self.state.get("real_idx") or {}).get(charger_id)
        if stored:
            return stored
        default = _int("RealKwhIdx", 0)
        return default or 0

    def _push_snapshot(self):
        sessions = sorted(self.state.get("sessions", {}).values(), key=lambda s: s.get("started_at") or "", reverse=True)[:SESSION_CAP]
        known = self.state.get("chargers", {})
        live = {c["id"]: c for c in self.chargers}
        chargers = []
        for cid, info in known.items():
            c = live.get(cid, {})
            chargers.append({
                "id": cid, "nickname": info.get("nickname"), "brand": info.get("brand"), "model": info.get("model"),
                "is_charging": c.get("is_charging", False), "charging_state": c.get("charging_state"),
                "power_kw": c.get("power_kw"), "connection_status": c.get("connection_status"),
                "mid_eligible": c.get("mid_eligible"), "help_url": c.get("help_url"),
                "last_seen_at": c.get("last_seen_at"), "latest_meter_wh": c.get("latest_meter_wh"),
                "current_session": c.get("current_session"), "real_idx": self.real_idx(cid),
                "energy_idx_device": charger_device_id(cid),
            })
        self._send({
            "type": "snapshot", "version": VERSION, "ts": int(time.time()), "chargers": chargers, "sessions": sessions,
            "estimate": self.state.get("estimate"), "ere": self.ere, "energy": self.energy,
            "settings": {"allow_control": _bool("AllowControl"), "allow_restart": _bool("AllowControl") and _bool("AllowRestart")},
            "setup": {"token_set": bool(Parameters.get("ApiToken")), "connected": bool(self.last_ok) and not self.api_error,
                      "import_done": bool(self.state.get("import_done")), "import_running": self.import_running,
                      "import_sessions": self.import_progress, "writes_pending": len(self.pending)},
            "error": self.api_error,
        })

    def onWebSocketMessage(self, Data):
        try:
            msg = json.loads(Data) if isinstance(Data, str) else Data
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        target = msg.get("hwid")
        if target is not None and str(target) != self.hwid:
            return
        cmd = msg.get("cmd")
        if cmd == "snapshot":
            self._push_snapshot()
        elif cmd == "refresh":
            self.next_run = {}
            self.backoff_until = 0
            self.auth_failed = False
            self._send({"type": "ack", "cmd": cmd})
        elif cmd == "import":
            self.state["import_done"] = False
            self.state["written"] = {}  # an explicit import rewrites every day, whatever is in the database now
            self.pending.clear()
            self.import_wanted = True
            self.next_run.pop("sessions", None)
            self.inflight.discard("sessions")
            self.auth_failed = False
            self._send({"type": "ack", "cmd": cmd})
        elif cmd == "setRealDevice":
            self._set_real_device(msg)
        elif cmd == "restart":
            self._restart(str(msg.get("charger_id", "")), bool(msg.get("hard")))
        else:
            self._send({"type": "error", "error": "unknown command"})

    def _set_real_device(self, msg):
        cid, idx = str(msg.get("charger_id", "")), msg.get("idx")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = 0
        if cid not in self.state.get("chargers", {}):
            self._send({"type": "error", "error": "unknown charger"})
            return
        self.state.setdefault("real_idx", {})[cid] = idx
        self.dirty = True
        self._send({"type": "ack", "cmd": "setRealDevice"})
        self._push_snapshot()

    def _restart(self, charger_id, hard):
        if not (_bool("AllowControl") and _bool("AllowRestart")):
            self._send({"type": "restart_result", "ok": False, "error": "Restart is not allowed in the plugin settings"})
            return
        if charger_id not in self.state.get("chargers", {}):
            self._send({"type": "restart_result", "ok": False, "error": "unknown charger"})
            return
        self.jobs.put(("restart", {"charger_id": charger_id, "hard": hard}))

    # ------------------------------------------------------------------ device commands
    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        if Unit == C_RESTART and DeviceID.startswith("JC") and Command in ("On", "Off"):
            cid = next((c for c in self.state.get("chargers", {}) if charger_device_id(c) == DeviceID), None)
            if cid:
                self._restart(cid, False)
            self._write_unit(DeviceID, Unit, 0, "Off")
        elif Unit == C_CHARGING:
            # read only, put the displayed state back
            live = next((c for c in self.chargers if charger_device_id(c["id"]) == DeviceID), None)
            on = bool(live and live.get("is_charging"))
            self._write_unit(DeviceID, Unit, 1 if on else 0, "On" if on else "Off")


global _plugin
_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onStop():
    _plugin.onStop()


def onHeartbeat():
    _plugin.onHeartbeat()


def onCommand(DeviceID, Unit, Command, Level, Color):
    _plugin.onCommand(DeviceID, Unit, Command, Level, Color)


def onWebSocketMessage(Data):
    _plugin.onWebSocketMessage(Data)
