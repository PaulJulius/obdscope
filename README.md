# obdscope

[![tests](https://github.com/PaulJulius/obdscope/actions/workflows/ci.yml/badge.svg)](https://github.com/PaulJulius/obdscope/actions/workflows/ci.yml)

**Read your car's data from a Veepeak OBDCheck BLE adapter** (or any other
ELM327-compatible Bluetooth Low Energy dongle) on macOS and Linux.

A command line tool and a web dashboard: live gauges, trouble codes, freeze
frames, fuel trims for chasing vacuum leaks, and codes from modules that
standard OBD-II can't reach. It works with any OBD-II vehicle, pre-CAN or
modern, and detects the protocol automatically. Two simulated vehicles let you
try all of it without a car.

New to this? You need the adapter plugged into the OBD-II port under the dash,
the ignition on, and `uv run obdscope ui`.

![The dashboard: live gauges for RPM, speed, coolant, load, throttle and fuel
trims, each with a trend line](docs/dashboard.png)

*The dashboard, connected to one of the built-in simulated vehicles.*

## Safety and scope

Reading data is harmless. Two commands are not passive, so know what they do:

- **`clear-dtc`** erases stored codes *and* resets the readiness monitors. A
  vehicle with incomplete monitors fails an emissions inspection until several
  days of driving complete them. It asks for confirmation first.
- **`probe`** sends manufacturer-specific requests (mode 22) to a module. It
  only reads, but these requests are undocumented and vary by vehicle.

Don't operate this while driving; have a passenger do it, or log to CSV and
read it afterwards. Diagnostics tell you what a vehicle reports, not whether
it is safe to drive. This software comes with no warranty (see [LICENSE](LICENSE)) — you
are responsible for what you do to your vehicle.

## What has been tested

- **Hardware:** one Veepeak OBDCheck BLE (reporting `ELM327 v1.5`), on macOS.
- **Vehicles:** a 2001 Ford Expedition XLT (J1850 PWM) — info, codes, live
  data, module sweeps and monitoring all confirmed against the real SUV.
- **Everything else,** including the whole CAN path, is exercised only against
  the simulator and scripted fakes. It follows the standards and the parsing is
  tested, but it has never met a real CAN vehicle. Reports welcome.

## Setup

```sh
uv sync
uv run obdscope --help
```

On macOS, the app that runs the command (Terminal, iTerm, VS Code) needs
Bluetooth permission: System Settings → Privacy & Security → Bluetooth.
You don't pair the adapter in System Settings. BLE adapters connect directly
from the app.

## Simulated vehicles

Add `--simulate rav4` or `--simulate expedition` to any command to use a
simulated vehicle instead of the adapter. You don't need the adapter,
Bluetooth, or the car:

```sh
uv run obdscope ui --simulate expedition     # or pick a vehicle next to Connect
uv run obdscope live --simulate rav4
uv run obdscope dtc --simulate expedition
uv run obdscope raw --simulate rav4          # type ATZ, 010C, 0902...
```

The simulator answers the same ELM327 commands as the real adapter, using
the same J1850 or CAN framing, so the parser, CLI and UI all run the code
they run against a real car. The vehicle repeats a two-minute drive: about
20 seconds of idle, city speed, highway, then slowing to a stop. It starts
cold, so coolant climbs and the fuel system switches from open to closed
loop.

| | 2001 Expedition XLT (`expedition`) | 2019 RAV4 Adventure (`rav4`) |
| --- | --- | --- |
| Protocol | J1850 PWM, one PCM (`10`) | CAN 11-bit, engine `7E8` + transmission `7E9` |
| Check engine light | On: P0171, P0174 (lean, both banks) | Off. Pending P0456 (tiny evap leak) |
| VIN | reported (as the real SUV does) | reported |
| What to notice | Long-term fuel trims around +14% at idle, near +3% at speed: a vacuum-leak pattern. Clear the codes and P0171 returns as pending about a minute later. | VIN, permanent codes, wideband O2 (`Wideband O2 B1S1 lambda`), catalyst temperature |
| `probe` finds | a few DIDs in `1100`–`11FF` | DIDs in `1000`–`10FF` (engine), `--header 7E1` for transmission |

The mode 22 identifiers are made up so that `probe` has something to find.
They don't match real Ford or Toyota identifiers. The simulated VIN
(`2T3SIMRAV4KW00001`) is also made up.

## Web dashboard

```sh
uv run obdscope ui
```

This opens a page in your browser with three tabs:

- **Dashboard:** live gauges with a short trend line for each. Add or remove
  gauges, change the sample rate, switch °F/°C, and record to CSV. Recordings
  are saved to `recordings/`.
  **Leak hunt** adds a panel with the total fuel correction per bank against
  a baseline, the same workflow as `obdscope trims` but readable from a phone
  while you're under the hood (`obdscope ui --host 0.0.0.0`).
- **Trouble codes:** stored, pending and permanent codes, and the freeze frame.
  Clearing codes asks for confirmation first.
- **Vehicle:** VIN, readiness monitors, supported PIDs, and a one-time read of
  every sensor.

![Leak hunt: total fuel correction per bank against a baseline, with a
leaner/richer track and a message reading "LEANER by 9.4% — whatever you just
blocked is (part of) the leak"](docs/leak-hunt.png)

The server only accepts connections from this Mac. To view the dashboard on a
phone on the same Wi-Fi, run `uv run obdscope ui --host 0.0.0.0` and open
`http://<mac-name>.local:8765/`. There's no password, so anyone on that
network could also clear codes. Only use `--host 0.0.0.0` on a network you
trust.

## Command line

Plug in the adapter, turn the ignition to ON (the engine can be off for most
commands), then:

```sh
uv run obdscope scan                 # find the adapter (marked with *)
uv run obdscope info                 # protocol, VIN, check-engine light, readiness monitors
uv run obdscope pids                 # read every supported sensor once
uv run obdscope live                 # stream RPM, speed, temps, fuel trims...
uv run obdscope live rpm maf o2b1s1 --interval 0.5 --csv drive.csv
uv run obdscope trims                # live fuel trims, for hunting vacuum leaks
uv run obdscope dtc                  # stored, pending, permanent codes and freeze frame
uv run obdscope clear-dtc            # clear codes (asks for confirmation)
uv run obdscope raw                  # interactive ELM327 console
uv run obdscope raw ATRV 010C        # one-shot raw commands
uv run obdscope sniff                # listen to the bus, list the modules talking
uv run obdscope modules              # ask every module on the bus for its codes
uv run obdscope probe 1100 11FF      # sweep manufacturer (mode 22) identifiers
```

Global options can go before or after the command:

| Option | Purpose |
| --- | --- |
| `--address UUID` | connect to a specific adapter (macOS shows a UUID, not a MAC) |
| `--protocol N` | skip auto-detect: `1` = J1850 PWM (older Fords), `6` = 11-bit CAN (most 2008+) |
| `--metric` | metric units (default is °F / mph / psi) |
| `--log FILE` | record every raw command and response, useful for figuring out odd behaviour |
| `--simulate rav4\|expedition` | use a simulated vehicle instead of the adapter (see below) |

`live` accepts aliases (`rpm speed coolant load throttle stft1 ltft1 stft2 ltft2
map iat maf timing o2b1s1 o2b1s2 o2b2s1 o2b2s2 fuel baro cat1 ambient pedal
fuelrate torque odometer runtime`) or hex PIDs (`0C`). PIDs the vehicle doesn't
support are skipped, so e.g. the bank 2 trims drop out on the RAV4's 4-cylinder.

### Hunting a vacuum leak with `trims`

Fuel trim is how much the computer corrects its fuel calculation: 0% means
no correction, positive means it's adding fuel because the engine is running
lean. Short term reacts within seconds; long term is what the computer has
learned. **Add them together** — that's the real correction, and beyond about
±10% something is wrong.

`obdscope trims` holds a baseline from the first few samples at idle, then
shows the change from it. Block a suspected leak and the engine needs less
extra fuel, so the reading drops and the monitor says so:

```
           short    long    total   change   leaner            richer
bank 1     +0.0%   +4.7%    +4.7%   -22.7%   ·#··········|············
engine rpm 715 rpm   coolant temperature 183 °F
>>> LEANER by 22.7% -- whatever you just blocked is (part of) the leak
```

To tell a vacuum leak from a sensor or fuel-supply problem, compare idle
with a held 2000 rpm. A fixed leak is a large share of the small airflow at
idle and a small share at higher airflow, so its correction shrinks; a MAF
or fuel-delivery fault stays about the same at both. Multiply the correction
by the airflow at each point: if a leak is the cause, both give the same
grams per second of unmetered air.

## What to expect from different vehicles

The protocol is detected automatically; these are the practical differences.

**Pre-CAN vehicles (roughly before 2008)** use J1850 PWM/VPW, ISO 9141 or KWP.
Usually one module answers, and it supports far fewer PIDs — often just the
20 or so covering load, temperatures, fuel trims, MAF/MAP, RPM, speed, timing,
throttle and O2 sensors. Mode 09 (VIN) wasn't required until about MY2005 and
may return nothing, and permanent codes (mode 0A) don't exist before 2010.
Expect the odd frame to fail its checksum on these buses; the tool drops it
and retries. Upstream O2 sensors are narrowband, switching between about
0.1V and 0.9V several times a second once in closed loop.

**CAN vehicles (2008 on)** support many more PIDs, report a VIN, and often
answer from several modules at once — commonly engine (`7E8`) and transmission
(`7E9`). The tool uses the engine's reply where one value is needed and labels
replies when several modules answer. Modern engines use a wideband upstream
sensor that reads as lambda (1.000 is ideal) rather than a switching voltage.

**Manufacturer data (mode 22)** is not part of standard OBD-II and isn't
published. `probe` sweeps a range of identifiers and lists whatever answers,
addressed to a specific module: `7E0` for the engine on CAN, `C4 10 F1` for a
Ford PCM on J1850 PWM. Working out what a response means is up to you, for
example by watching it change as the engine warms up. Probing only reads.

**Other modules (ABS, restraints, body)** are not part of OBD-II, so `dtc`
won't show their faults. Where they share the bus you can often reach them:
`sniff` lists the addresses that are talking (it only listens, and transmits
nothing), then address one directly with `raw`. Manufacturers use their own
services for this — older Fords read codes with mode `13` rather than `03`:

```sh
uv run obdscope sniff       # which modules are transmitting
uv run obdscope modules     # ask each address for codes and decode them
```

`modules` sweeps every address (a couple of minutes on a pre-CAN bus) and
decodes what comes back. Codes from these modules are manufacturer-specific:
the letter says which system (B = body, C = chassis, U = network), and you'll
need a marque-specific list to look most of them up.

Be aware that many vehicles put safety and body modules on a separate network
whose pins a standard ELM327 adapter doesn't wire up. If a module never
answers, it may simply be unreachable with this hardware.

**Your own vehicle notes:** `notes/` is gitignored, a place to keep VINs,
supported PIDs, measurements and findings per vehicle without committing them.

## Related projects

[python-OBD](https://github.com/brendan-w/python-OBD) is the established
library in this space and supports more adapters and PID definitions. This
project differs in being BLE-native (no serial port pairing, which is what
macOS needs for these adapters), in shipping a web dashboard and full vehicle
simulators, and in reaching non-powertrain modules.

This is a hobby project, maintained as time allows. Bug reports are welcome,
but most vehicle-specific issues can't be reproduced without that vehicle —
a `--log` transcript makes them far more actionable.

## Development

```sh
uv run pytest
```

Tests use scripted fake adapters (`tests/fakes.py`) and the simulator, so no
hardware is needed. Code layout:

- `transport.py`: BLE connection and GATT notify/write handling
- `elm327.py`: AT commands, error handling, J1850/CAN frame parsing
- `obd.py`: OBD services (modes 01, 02, 03/07/0A, 04, 09, 22), ECU naming
- `pids.py` and `dtc.py`: decoders and descriptions
- `reports.py`: multi-request reports (vehicle info, all sensors, trouble codes) shared by CLI and UI
- `cli.py`: the `obdscope` command
- `web.py` and `static/index.html`: the `obdscope ui` server and single-page dashboard
- `simulator.py`: simulated adapter and vehicles (`--simulate`)
