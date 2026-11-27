# Elco / Ariston NET remotethermo for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant integration for **Ariston NET / remotethermo** boilers and heat pumps (Ariston, Elco, ...),
tuned for the **Elco / Ariston Galevo hybrid** systems.

This is a fork of [fustom/ariston-remotethermo-home-assistant-v3](https://github.com/fustom/ariston-remotethermo-home-assistant-v3)
(upstream 0.20.7), which in turn was inspired by
[chomupashchuk's v2 integration](https://github.com/chomupashchuk/ariston-remotethermo-home-assistant-v2).
It talks to the real Ariston cloud API (not the website), is set up from the UI, and creates devices and entities.
All the upstream entities are kept; the additions are listed below.

## What this fork adds

### Consumption sensors (Galevo)
Electricity and gas, split in central heating and domestic hot water, for **today**, **yesterday**,
**last 30 days** and **last month**, plus heating + hot water totals and **"since start"** counters.

- Gas is shown in **m³ (Smc)**; the cloud only provides kWh, so it is converted with a configurable factor
  (*Options → gas kWh per Smc*, default `10.69`, range 5-15). The kWh value is kept as the `kwh` attribute.
- The cloud keeps about 21 months of history, so the "since start" counters are built locally:
  closed months are added once to a persisted base and the current month is added from the daily values.
  They never decrease, even across restarts or long outages.
- Sensors are only created for the consumption types your cloud account actually returns.
- The old, inaccurate consumption sensors are removed automatically on upgrade.

### Extra read-only sensors
Values read from the cloud menu items and plant data (for example heating flow offset, extra temperatures and
counters). Entities that the cloud does not return for your system are not created.

### Writable menu settings (Galevo)
Settings of the "6.x" menu of the Ariston NET website, written through `dataItems/set` using the portal id
(e.g. `U_6.15.2` → `U6_15_2`):

| Entity | Portal id | Type |
| --- | --- | --- |
| PV DHW delta | `U6_15_2` | number |
| Winter auto z1 / threshold / delay | `U6_3_5_0_0/1/2` | switch / numbers |
| Sanitization / interval | `U6_9_5_0/1` | switch / number |
| Tank charge mode | `U6_9_3` | select |
| Energy manager logic | `U6_12_0` | select |

Only user-level settings are exposed, never installer parameters. Each entity has a `source` attribute:
`cloud` (value read from the cloud), `last written` (value set from Home Assistant, restored after a restart)
or `initial (assumed)`.

> **Note:** only `U6_15_2` has been verified on a real system. The other ids, and the limits of the numbers,
> are conservative assumptions. Please report anything that does not behave as expected.

### Robustness
- The ariston library uses the **system DNS resolver**, which avoids c-ares "Timeout while contacting DNS servers" errors.
- Cloud errors (HTTP 500, timeouts) make the affected coordinator unavailable and are retried, instead of
  producing tracebacks or breaking the other entities.

## Installation

### HACS (custom repository)
1. HACS → ⋮ → *Custom repositories* → add `https://github.com/superkekko/elco-remotethermo`, category *Integration*.
2. Install **Ariston** and restart Home Assistant.
3. *Settings → Devices & services → Add integration → Ariston*.

### Manual
Copy `custom_components/ariston` to your `config/custom_components` folder, restart and add the integration from the UI.

If you use the original integration, remove it first: both use the `ariston` domain.

## Compatibility
Upstream was tested on Ariston Alteas One 24, Velis Evo, Velis Lux and Lydos Hybrid. The fork's additions were
developed and verified on an Elco / Ariston **Galevo hybrid**. Other systems keep the upstream behaviour; feel free
to open an issue or a pull request.

## Development
The test suite uses Home Assistant's pytest fixtures and requires at least 90% coverage. It runs on Python 3.14.2, matching CI.

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements_test.txt
python -m pytest
```

To update test dependencies, edit `requirements_test.in` and regenerate the hashed lock:

```sh
uv pip compile requirements_test.in --python 3.14.2 --python-version 3.14.2 --universal --generate-hashes --upgrade --output-file requirements_test.txt
```

## Credits
[fustom](https://github.com/fustom) for the upstream integration and the reverse-engineered API library,
[chomupashchuk](https://github.com/chomupashchuk) for the original v2 work.
