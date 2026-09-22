# veepeak-reader

A command-line tool for reading OBD-II data through a **Veepeak OBDCheck BLE**
(ELM327-compatible, Bluetooth Low Energy) adapter. It was built for a 2001 Ford
Expedition XLT but works with any OBD-II vehicle.

## Setup

```sh
uv sync
uv run veepeak --help
```

On macOS, the app that runs the command (Terminal, iTerm, VS Code) needs
Bluetooth permission: System Settings → Privacy & Security → Bluetooth.
You don't pair the adapter in System Settings. BLE adapters connect directly
from the app.

## Usage

Plug in the adapter, turn the ignition to ON (the engine can be off for most
commands), then:

```sh
uv run veepeak scan                 # find the adapter (marked with *)
uv run veepeak info                 # protocol, VIN, check-engine light, readiness monitors
uv run veepeak pids                 # read every supported sensor once
uv run veepeak live                 # stream RPM, speed, temps, fuel trims...
uv run veepeak live rpm maf o2b1s1 --interval 0.5 --csv drive.csv
uv run veepeak dtc                  # stored + pending trouble codes and freeze frame
uv run veepeak clear-dtc            # clear codes (asks for confirmation)
uv run veepeak raw                  # interactive ELM327 console
uv run veepeak raw ATRV 010C        # one-shot raw commands
uv run veepeak probe 1100 11FF      # sweep Ford mode 22 identifiers
```

Global options go **before** the command:

| Option | Purpose |
| --- | --- |
| `--address UUID` | connect to a specific adapter (macOS shows a UUID, not a MAC) |
| `--protocol 1` | skip auto-detect and use J1850 PWM directly (a little faster on the Expedition) |
| `--metric` | metric units (default is °F / mph / psi) |
| `--log FILE` | record every raw command and response, useful for figuring out odd behaviour |

`live` accepts aliases (`rpm speed coolant load throttle stft1 ltft1 stft2 ltft2
map iat maf timing o2b1s1 o2b1s2 o2b2s1 o2b2s2 fuel baro ambient runtime`) or
hex PIDs (`0C`).

## Notes on the 2001 Expedition

- **Protocol:** The PCM uses **SAE J1850 PWM** (ELM327 protocol 1). The tool
  turns headers on, so each reply shows which module answered. The PCM is
  address `10`.
- **VIN:** Mode 09 (vehicle info) wasn't required until about MY2005, so
  `info` will probably say the VIN isn't reported.
- **Supported PIDs:** Expect roughly 15–20 standard PIDs: load, coolant temp,
  fuel trims, MAP/MAF, RPM, speed, timing, IAT, throttle and O2 sensors.
  Newer PIDs such as fuel level and ambient temperature are usually missing.
- **Useful for diagnosis:** Fuel trims (`stft1 ltft1 stft2 ltft2`) are the
  place to start with lean codes like P0171/P0174, which are common on these
  trucks (often a vacuum leak or a dirty MAF). If long-term trims stay above
  about +10% at idle but drop toward 0 at 2500 rpm, suspect a vacuum leak.
- **Enhanced Ford data (mode 22):** Transmission temperature and similar values
  are Ford-specific and not part of standard OBD-II. `probe` sends mode 22
  requests with the header `C4 10 F1` (sent directly to the PCM) and lists
  every identifier that answers. You'll have to work out what each value
  means yourself, for example by watching it change while the truck warms up.
  Probing only reads data. It never writes to the PCM.

## Development

```sh
uv run pytest
```

Tests use a scripted fake adapter (`tests/test_obd.py`), so no hardware is
needed. Code layout:

- `transport.py`: BLE connection and GATT notify/write handling
- `elm327.py`: AT commands, error handling, J1850/CAN frame parsing
- `obd.py`: OBD services (modes 01, 02, 03/07, 04, 09, 22)
- `pids.py` and `dtc.py`: decoders and descriptions
- `cli.py`: the `veepeak` command
