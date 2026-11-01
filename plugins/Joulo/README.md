# Joulo plugin for Domoticz

Brings your [Joulo](https://joulo.nl) EV charging data into Domoticz: live charger status, charged energy, ERE credits
and ERE yield (with history), the ERE position of your account, alerts, and a ready-made page.

## Setup (3 steps)

1. In the Joulo dashboard open **Developer, API** and create an API token.
2. In Domoticz add hardware of type **Joulo (EV charging ERE yield)** and paste the token. Everything else has a good default.
3. Open the page **`#/Custom/Joulo`** (for example `http://domoticz:8080/#/Custom/Joulo`). Until all four steps there are green it opens on the
   **Setup** tab: connection test, charger found, pick your real charger kWh device (for the cost figures), import history.

Requires a Domoticz version with the extended plugin settings (checkbox, number and password fields) and the Unit `Adjustment2` property.

## What you get

Per charger (devices, all with history and graphs):

| Device | Type | Notes |
|---|---|---|
| energy | Managed counter (kWh) | charged energy, history per day |
| ERE yield | Managed counter (EUR) | indicative net value of the ERE credits, history per day |
| ERE credits | Managed counter | history per day |
| power, session energy, meter reading | Custom sensors | live |
| status | Text | Charging / online / offline |
| charging | Switch | read only |
| connection | Alert | goes orange or red when the charger is offline, usable for notifications |
| restart | Push button | only when Allow control and Allow restart are on |

Account devices: total energy, ERE credits and sessions, ERE paid, payable, reserved, unsold forecast, expected this year,
price per ERE, current quarter, an **ERE status** alert (blocked reasons such as review, mandate, iban) and a **connection**
alert (token rejected, rate limited, Joulo unreachable).

## History import

The first start imports all sessions. Sessions are totalled per local day and written as dated managed counter rows, so
the Domoticz day, week, month and year graphs work. The totals are recalculated from the complete session set every time, so
importing twice never doubles anything, and existing days are overwritten. Use **Re-import history** on the page or the
Setup tab to repeat it. Days that were written before but no longer have sessions are set to zero.

Managed counters only store whole numbers, so the plugin stores energy in Wh (divider 1000), ERE credits in milli ERE
(divider 1000) and euro in cents (divider 100).

## The page

Tabs: Overview (day, week, month, year with totals and a chart), Sessions (date, duration, kWh, cost, ERE, net cost),
Insights, ERE yield (your ERE position) and Setup. Cost comes from the real charger device you select (its daily price from
the Domoticz graphs). A session on a day with several sessions gets an estimated cost, marked with `~`. Net cost is cost
minus ERE yield. The optional **Create a Dynamic Dashboard** button in the Setup tab builds a Dashboard Dynamic layout named "Joulo" from your devices: per charger the power, energy, ERE yield and ERE credits, charts per day and per month, and your ERE position. Pressing it again adds another copy.

## Settings

| Setting | Meaning |
|---|---|
| API token | from the Joulo dashboard, never logged |
| Import history on first start | on by default |
| Real charger kWh device | optional dropdown with your energy meters, can also be picked on the page |
| Allow control / Allow restart | adds a restart button (Joulo only supports restarting OCPP chargers) |
| Charger poll | 60 s: charger status (online, charging, power, running session) |
| Session poll while charging | 60 s: session list, so day totals and history follow the running session |
| Session poll while idle | 1 h: session list when nothing is charging |
| Account and ERE poll | 1 h: account totals and ERE position, Joulo advises about hourly |
| Debug logging | logs every API call (never the token) and history write |

Plugin state (session cache, chosen real devices) is kept in `<userdata>/joulo-data/`.

## Tests

`python -m unittest discover -s plugins/Joulo/tests` runs the unit tests for the API client and the aggregation (no Domoticz needed).
