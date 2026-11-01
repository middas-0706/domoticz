#!/usr/bin/env python3
"""Verify that barometer history keeps the right scale, also across database upgrades.

Purpose
-------
Barometer values are stored in two scales, depending on the device type:

  - Temp+Hum+Baro sensors other than THBFloat (sTypeTHB1, sTypeTHB2) store
    plain hPa (1013).
  - Every other barometer type (THBFloat, Temp+Baro, General Barometer and
    the Thermostat6 baro subtypes) stores hPa * 10 (10130).

The graph code divides by 10 per type when it shows the values. Upgrade step
176 once assumed every barometer had moved to the *10 scale and multiplied all
Temperature_Calendar values below 8500 by 10, which made the history of the
plain hPa sensors 10 times too high (forum topic 45303). Step 184 undoes that.

This script checks, per barometer device type:

  1. A value sent with udevice is shown as the same hPa in the device list,
     in the day graph (Temperature short log, written by Domoticz itself) and
     in the month graph (Temperature_Calendar).
  2. The month graph still shows the same hPa after every database upgrade
     step has run (DB_Version reset to 175), including a high altitude value
     below 850 hPa. This is the check that fails with the old step 176.
  3. Calendar values left multiplied by the old step 176 are restored by the
     upgrade (DB_Version reset to 183).

The Temperature_Calendar row of a day is normally written at midnight. The
script writes it the same way Domoticz does (AVG(Barometer) of that day's
short log rows), so it does not have to wait for midnight.

Usage
-----
    python test_barometer.py <path-to-domoticz.exe>

It starts its own Domoticz on a free port with a throwaway database and
userdata folder, so it never touches an existing installation. Domoticz takes
a single-instance mutex, so no other instance may be running (see
test/README.md). The run takes about two minutes: it waits for Domoticz to
write the short log, with the short log interval set to one minute.
"""
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse

from test_calibration import (EarlyExit, add_dummy_hardware, cmd, create_device, get_device,
                              login, push_udevice, repo_root, start_domoticz, wait_for_no_domoticz)

HPA = 1013.0
HPA_ALTITUDE = 845.0     # a sensor at about 1500 m; stored *10 this is below 8500

# (label, type, subtype, udevice sValue, stored scale)
DEVICES = [
    ("TEMP_HUM_BARO THB1", 84, 1, "20.0;50;1;%d;0" % HPA, 1),
    ("TEMP_HUM_BARO THBFloat", 84, 16, "20.0;50;1;%.1f;0" % HPA, 10),
    ("TEMP_BARO", 247, 1, "20.0;%.1f;0;0" % HPA, 10),
    ("General Barometer", 243, 26, "%.1f;0" % HPA, 10),
    ("Thermostat6 Temp/Baro", 73, 2, "20.0;21.0;%.1f" % HPA, 10),
    ("Thermostat6 Temp/Hum/Baro", 73, 3, "20.0;21.0;50;1;%.1f" % HPA, 10),
]

CHECKS = 0
FAILURES = 0


def check(label, what, expected, observed, tol=0.6):
    global CHECKS, FAILURES
    CHECKS += 1
    try:
        ok = abs(float(observed) - expected) <= tol
    except (TypeError, ValueError):
        ok = False
    if not ok:
        FAILURES += 1
    print("  %-4s  %-28s  %-34s expected=%-8s observed=%s" % ("PASS" if ok else "FAIL", label, what, expected, observed))


def restart(domo):
    """Stop the instance and start it again on the same database and port."""
    domo.stop()
    wait_for_no_domoticz()
    domo.log = open(os.path.join(domo.tmp, "domoticz.log"), "a", encoding="utf-8", errors="replace")
    args = [domo.exe, "-www", str(domo.port), "-wwwbind", "127.0.0.1", "-sslwww", "0",
            "-dbase", domo.db, "-userdata", domo.tmp, "-wwwroot", os.path.join(domo.root, "www"),
            "-nobrowser", "-noupdates", "-loglevel", "debug"]
    domo.proc = subprocess.Popen(args, stdout=domo.log, stderr=subprocess.STDOUT, cwd=domo.root)
    domo._wait_ready()
    domo.cookie_header = login(domo)


def db_write(domo, statements):
    """Run SQL on the database while Domoticz is stopped."""
    domo.stop()
    wait_for_no_domoticz()
    c = sqlite3.connect(domo.db)
    for sql, params in statements:
        c.execute(sql, params)
    c.commit()
    c.close()


def graph_ba(domo, idx, rng):
    code, data = cmd(domo, "/json.htm?type=command&param=graph&sensor=temp&idx=%d&range=%s" % (idx, rng))
    return {r.get("d", "")[:10]: r.get("ba") for r in data.get("result") or []}


def wait_for_short_log(domo, ids, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = sqlite3.connect(domo.db)
        n = c.execute("SELECT COUNT(DISTINCT DeviceRowID) FROM Temperature WHERE DeviceRowID IN (%s)"
                      % ",".join(str(i) for i in ids)).fetchone()[0]
        c.close()
        if n == len(ids):
            return True
        time.sleep(5)
    return False


def main():
    global FAILURES
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    exe = args[0]
    root = repo_root(exe) if os.path.exists(exe) else None
    if not root:
        print("domoticz executable or checkout not found: %s" % exe)
        return 2
    print("testing: %s" % os.path.abspath(exe))

    try:
        domo = start_domoticz(exe, root, None)
    except EarlyExit as e:
        print("could not start domoticz: %s" % e)
        return 2
    domo.root = root
    try:
        print("domoticz running on port %d (temp data in %s)" % (domo.port, domo.tmp))
        domo.cookie_header = login(domo)
        if not domo.cookie_header:
            print("ERROR: could not create/log in as an admin user")
            return 2

        hw = add_dummy_hardware(domo, "BarometerTest")
        devices = []
        for label, typ, sub, svalue, scale in DEVICES:
            try:
                idx = create_device(domo, hw, label, typ, sub)
            except RuntimeError as e:
                print("  FAIL  %-28s  could not create the device: %s" % (label, e))
                FAILURES += 1
                continue
            devices.append((label, idx, svalue, scale))

        # A one minute short log interval, so Domoticz writes the Temperature table soon
        db_write(domo, [("UPDATE Preferences SET nValue = 1 WHERE Key = 'ShortLogInterval'", ())])
        restart(domo)
        for label, idx, svalue, scale in devices:
            push_udevice(domo, idx, 0, svalue)

        print("\n=== Current value and short log (written by Domoticz) ===")
        for label, idx, svalue, scale in devices:
            dev = get_device(domo, idx)
            check(label, "device Barometer", HPA, dev.get("Barometer") if dev else None)
        print("  (waiting for the short log, at most 150 s)")
        if not wait_for_short_log(domo, [d[1] for d in devices]):
            print("  FAIL  the short log was not written in time")
            FAILURES += 1
        for label, idx, svalue, scale in devices:
            # updates keep LastUpdate current so a late short log run is not skipped
            push_udevice(domo, idx, 0, svalue)
            values = list(graph_ba(domo, idx, "day").values())
            check(label, "day graph ba", HPA, values[-1] if values else None)

        # Day rows the way the midnight job writes them: the average of the short log
        print("\n=== Month graph (Temperature_Calendar) ===")
        stmts = []
        for label, idx, svalue, scale in devices:
            stmts.append(("INSERT INTO Temperature_Calendar (DeviceRowID, Temp_Min, Temp_Max, Temp_Avg, Barometer, Date) "
                          "SELECT DeviceRowID, MIN(Temperature), MAX(Temperature), AVG(Temperature), AVG(Barometer), date('now', '-2 day') "
                          "FROM Temperature WHERE DeviceRowID = ?", (idx,)))
            # a high altitude day, stored in the scale of the device type
            stmts.append(("INSERT INTO Temperature_Calendar (DeviceRowID, Temp_Min, Temp_Max, Temp_Avg, Barometer, Date) "
                          "VALUES (?, 10, 14, 12, ?, date('now', '-3 day'))", (idx, int(HPA_ALTITUDE * scale))))
        db_write(domo, stmts)
        restart(domo)
        day2 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
        day3 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
        for label, idx, svalue, scale in devices:
            ba = graph_ba(domo, idx, "month")
            check(label, "month graph ba", HPA, ba.get(day2))
            check(label, "month graph ba (altitude)", HPA_ALTITUDE, ba.get(day3))

        print("\n=== After all database upgrade steps (DB_Version 175) ===")
        db_write(domo, [("UPDATE Preferences SET nValue = 175 WHERE Key = 'DB_Version'", ())])
        restart(domo)
        for label, idx, svalue, scale in devices:
            ba = graph_ba(domo, idx, "month")
            check(label, "month graph ba", HPA, ba.get(day2))
            check(label, "month graph ba (altitude)", HPA_ALTITUDE, ba.get(day3))

        print("\n=== History left multiplied by the old step 176 is restored (DB_Version 183) ===")
        stmts = [("UPDATE Preferences SET nValue = 183 WHERE Key = 'DB_Version'", ())]
        for label, idx, svalue, scale in devices:
            stmts.append(("UPDATE Temperature_Calendar SET Barometer = Barometer * 10 "
                          "WHERE DeviceRowID = ? AND Barometer > 0 AND Barometer < 8500", (idx,)))
        db_write(domo, stmts)
        restart(domo)
        for label, idx, svalue, scale in devices:
            ba = graph_ba(domo, idx, "month")
            check(label, "month graph ba", HPA, ba.get(day2))
            check(label, "month graph ba (altitude)", HPA_ALTITUDE, ba.get(day3))
    finally:
        domo.cleanup()

    print("\n%d checks, %d failure(s)" % (CHECKS, FAILURES))
    return 0 if FAILURES == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
