# veepeak-reader

A command-line tool for reading OBD-II data through a **Veepeak OBDCheck BLE**
(ELM327-compatible, Bluetooth Low Energy) adapter. It's used with a 2001 Ford
Expedition XLT and a 2019 Toyota RAV4 Adventure, but works with any OBD-II
vehicle. The protocol is detected automatically.

## Setup

```sh
uv sync
uv run veepeak --help
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
uv run veepeak ui --simulate expedition     # or pick a vehicle next to Connect
uv run veepeak live --simulate rav4
uv run veepeak dtc --simulate expedition
uv run veepeak raw --simulate rav4          # type ATZ, 010C, 0902...
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
| VIN | reported (as the real truck does) | reported |
| What to notice | Long-term fuel trims around +14% at idle, near +3% at speed: a vacuum-leak pattern. Clear the codes and P0171 returns as pending about a minute later. | VIN, permanent codes, wideband O2 (`Wideband O2 B1S1 lambda`), catalyst temperature |
| `probe` finds | a few DIDs in `1100`–`11FF` | DIDs in `1000`–`10FF` (engine), `--header 7E1` for transmission |

The mode 22 identifiers are made up so that `probe` has something to find.
They don't match real Ford or Toyota identifiers. The simulated VIN
(`2T3SIMRAV4KW00001`) is also made up.

## Web dashboard

```sh
uv run veepeak ui
```

This opens a page in your browser with three tabs:

- **Dashboard:** live gauges with a short trend line for each. Add or remove
  gauges, change the sample rate, switch °F/°C, and record to CSV. Recordings
  are saved to `recordings/`.
  **Leak hunt** adds a panel with the total fuel correction per bank against
  a baseline, the same workflow as `veepeak trims` but readable from a phone
  while you're under the hood (`veepeak ui --host 0.0.0.0`).
- **Trouble codes:** stored, pending and permanent codes, and the freeze frame.
  Clearing codes asks for confirmation first.
- **Vehicle:** VIN, readiness monitors, supported PIDs, and a one-time read of
  every sensor.

The server only accepts connections from this Mac. To view the dashboard on a
phone on the same Wi-Fi, run `uv run veepeak ui --host 0.0.0.0` and open
`http://<mac-name>.local:8765/`. There's no password, so anyone on that
network could also clear codes. Only use `--host 0.0.0.0` on a network you
trust.

## Command line

Plug in the adapter, turn the ignition to ON (the engine can be off for most
commands), then:

```sh
uv run veepeak scan                 # find the adapter (marked with *)
uv run veepeak info                 # protocol, VIN, check-engine light, readiness monitors
uv run veepeak pids                 # read every supported sensor once
uv run veepeak live                 # stream RPM, speed, temps, fuel trims...
uv run veepeak live rpm maf o2b1s1 --interval 0.5 --csv drive.csv
uv run veepeak trims                # live fuel trims, for hunting vacuum leaks
uv run veepeak dtc                  # stored, pending, permanent codes and freeze frame
uv run veepeak clear-dtc            # clear codes (asks for confirmation)
uv run veepeak raw                  # interactive ELM327 console
uv run veepeak raw ATRV 010C        # one-shot raw commands
uv run veepeak probe 1100 11FF      # sweep manufacturer (mode 22) identifiers
```

Global options can go before or after the command:

| Option | Purpose |
| --- | --- |
| `--address UUID` | connect to a specific adapter (macOS shows a UUID, not a MAC) |
| `--protocol N` | skip auto-detect: `1` for the Expedition (J1850 PWM), `6` for the RAV4 (CAN) |
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

`veepeak trims` holds a baseline from the first few samples at idle, then
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

## Notes on the 2001 Expedition

- **Protocol:** The PCM uses **SAE J1850 PWM** (ELM327 protocol 1). The tool
  turns headers on, so each reply shows which module answered. The PCM is
  address `10`.
- **VIN:** Mode 09 (vehicle info) wasn't required until about MY2005, but
  this truck does report its VIN, and `info` shows it. Other vehicles of
  the same era often don't; the tool handles either.
- **Supported PIDs:** Expect roughly 15–20 standard PIDs: load, coolant temp,
  fuel trims, MAP/MAF, RPM, speed, timing, IAT, throttle and O2 sensors.
  Newer PIDs such as fuel level and ambient temperature are usually missing.
- **Useful for diagnosis:** Fuel trims are the place to start with lean codes
  like P0171/P0174, which are common on these trucks (often a vacuum leak or
  a dirty MAF). `veepeak trims` is built for this; see above.
- **Enhanced Ford data (mode 22):** Transmission temperature and similar values
  are Ford-specific and not part of standard OBD-II. `probe` sends mode 22
  requests with the header `C4 10 F1` (sent directly to the PCM) and lists
  every identifier that answers. You'll have to work out what each value
  means yourself, for example by watching it change while the truck warms up.
  Probing only reads data. It never writes to the PCM.

## Notes on the 2019 RAV4 Adventure

- **Protocol:** The RAV4 uses **ISO 15765-4 CAN, 11-bit, 500 kbaud** (ELM327
  protocol 6). Several modules can answer the same request, commonly the
  engine (`7E8`) and transmission (`7E9`). For single values (`live`, freeze
  frame) the tool uses the engine's answer. `pids` and `info` label each
  reply with the module that sent it.
- **VIN:** The RAV4 reports it, so `info` shows it.
- **More data:** Expect many more PIDs than the Expedition. These include
  catalyst temperature (`cat1`), accelerator pedal, fuel level, ambient
  temperature, control module voltage, and possibly odometer (`odometer`,
  PID A6, which only some 2019+ vehicles support). The 2.5L engine's upstream
  O2 sensor is a wideband air-fuel sensor, so it shows up as `Wideband O2 B1S1
  lambda` (PID 24 or 34) rather than a 0–1 V reading. 1.000 is stoichiometric.
- **Permanent codes:** `dtc` also lists permanent codes (mode 0A, MY2010+).
  These can't be cleared with `clear-dtc`. The car clears them itself after
  the fault is repaired and the related monitor passes.
- **Enhanced Toyota data (mode 22):** On CAN, `probe` sends its requests
  straight to the engine computer (ID `7E0`) instead of broadcasting. Toyota's
  mode 22 identifiers aren't published, so treat the output as raw material to
  investigate. To probe the transmission, use `--header 7E1`.

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
- `cli.py`: the `veepeak` command
- `web.py` and `static/index.html`: the `veepeak ui` server and single-page dashboard
- `simulator.py`: simulated adapter and vehicles (`--simulate`)
