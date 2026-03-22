# Marstek Battery Controller

Controls **1–3 Marstek Venus E** batteries on a single phase using Home Assistant and AppDaemon. Reads real-time grid power from a **Shelly Pro 3EM** and distributes charge/discharge setpoints across all batteries to achieve **zero grid feed-in**.

Power is split proportionally to each battery's available SoC so all units stay balanced automatically. Batteries that go offline are detected and excluded from the split; they rejoin automatically when they recover.

> **Disclaimer:** This software is provided as-is for personal use. It directly controls physical hardware and interacts with your electrical installation. The authors take no responsibility for any damage to equipment, unintended grid feed-in, energy costs, or any other consequences arising from its use. Always verify your setup complies with local regulations and your energy provider's terms. Use at your own risk.

---

## How it works

```
Shelly Pro 3EM  ──►  Home Assistant  ──►  Marstek #1  (Modbus TCP)
  (grid power)       AppDaemon app   ├──►  Marstek #2  (Modbus TCP)
                                     └──►  Marstek #3  (Modbus TCP, optional)
```

Every second (configurable):

1. Read grid power from Shelly — positive = importing, negative = exporting
2. Read SoC from each battery — exclude any that are offline
3. Split total setpoint between available batteries weighted by SoC capacity
4. Write `force_mode` + power setpoint to each battery via Modbus TCP (parallel)

---

## Project structure

```
ha-marstek-controller/
├── LICENSE
├── README.md
├── .gitignore
├── config.example.yaml          ← template — copy to config.yaml and fill in
├── config.yaml                  ← your settings (gitignored — contains token)
├── deploy.py                    ← run locally to generate ha/ files from config.yaml
├── requirements.txt
├── requirements-dev.txt
│
├── controller/
│   └── battery_controller.py    ← AppDaemon app source
│
├── ha/                          ← files to copy to Home Assistant (see Setup)
│   ├── appdaemon.yaml           → /config/appdaemon.yaml
│   ├── apps.yaml                → /addon_configs/a0d7b954_appdaemon/apps/  (generated, gitignored)
│   └── packages/
│       └── testing_helpers.yaml → /config/packages/
│       (marstek.yaml            → /config/packages/  — generated, gitignored)
│
└── tests/
    ├── conftest.py
    ├── test_battery_controller.py
    └── test_scenarios.py        ← push mock values to HA to verify controller behaviour
```

---

## Requirements

| Component | Notes |
|-----------|-------|
| Home Assistant | With Modbus integration enabled |
| AppDaemon add-on | Install from HA Supervisor add-on store |
| Shelly Pro 3EM | Native HA integration |
| Marstek Venus E ×1–3 | **v1/v2 firmware** — see note below for v3 |
| Static IPs | Assign fixed IPs to each Marstek unit in your router |

> **Firmware note:** This uses the tested v1/v2 register map.
> For v3 firmware, `battery_soc` is at register `34002` with scale `0.1` instead of `32104`/`1`.

---

## Setup

All steps 1–3 run **on your local machine** (the same computer where this project lives), not on Home Assistant.

### 1. Configure

```bash
cp config.example.yaml config.yaml
# edit config.yaml — fill in IPs, Shelly entity ID, HA token
```

**Finding your Shelly entity ID:**
HA → Settings → Devices & Services → Shelly → your device → Entities.
Look for an entity with "active power" in the name, e.g. `sensor.shelly_pro3em_aabbcc_total_active_power`.

**Verifying `power_sign`:**
In HA Developer Tools → States, find your Shelly entity. Turn on a large load — the value should go **positive** (importing). If it goes negative, set `power_sign: -1`.

### 2. Generate HA files

Run this on your local machine inside the project folder:

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

python deploy.py
```

This reads `config.yaml` and writes two files:
- `ha/appdaemon/apps/apps.yaml` — AppDaemon app configuration
- `ha/packages/marstek.yaml` — HA Modbus sensors and template sensors

Re-run `deploy.py` whenever you change `config.yaml`.

### 3. Deploy to Home Assistant

The AppDaemon add-on uses its **own isolated config directory** (`/addon_configs/a0d7b954_appdaemon/`), separate from HA's `/config/`. You need two tools:

- **Terminal add-on** — to write to the AppDaemon directory
- **File Editor add-on** — to write to `/config/packages/`

#### AppDaemon files (via Terminal add-on)

```bash
cp /config/marstek/controller/battery_controller.py /addon_configs/a0d7b954_appdaemon/apps/
cp /config/marstek/ha/apps.yaml /addon_configs/a0d7b954_appdaemon/apps/
```

> Replace `/config/marstek/` with wherever you placed this project on your HA instance,
> or upload the files manually via the File Editor first.

#### HA config files (via File Editor)

```
ha/appdaemon.yaml                 →  /config/appdaemon.yaml
ha/packages/marstek.yaml          →  /config/packages/marstek.yaml
ha/packages/testing_helpers.yaml  →  /config/packages/testing_helpers.yaml
```

Enable packages in `configuration.yaml` if not already done:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

### 4. Install and start AppDaemon

HA → Settings → Add-ons → Add-on Store → search **AppDaemon** → Install → Start → enable **Start on boot**.

### 5. Restart HA and verify

After restarting HA, check **AppDaemon logs** (HA → Settings → Add-ons → AppDaemon → Log):

```
BatteryController started — 2 batteries
marstek_1: remote control enabled
marstek_2: remote control enabled
```

---

## Testing without hardware

Create mock sensors in HA and push test values from your local machine:

```bash
# Simulate all scenarios (15 s each), watch AppDaemon logs while it runs
python test_scenarios.py

# Or set values manually
python test_scenarios.py --set-grid -800          # 800 W solar excess → charge
python test_scenarios.py --set-grid 600           # 600 W import → discharge
python test_scenarios.py --set-soc1 80 --set-soc2 20
```

Set `shelly.entity_id: input_number.mock_grid_power` in `config.yaml` while testing.
The mock input_number helpers are defined in `ha/packages/testing_helpers.yaml`.

---

## Configuration reference

| Key | Default | Description |
|-----|---------|-------------|
| `homeassistant.url` | — | HA base URL |
| `homeassistant.token` | — | Long-lived access token |
| `batteries[].id` | — | Hub name (used in entity IDs) |
| `batteries[].name` | — | Display name |
| `batteries[].ip` | — | Static IP of the Marstek unit |
| `batteries[].port` | `502` | Modbus TCP port |
| `batteries[].slave` | `1` | Modbus slave ID |
| `shelly.entity_id` | — | HA entity for grid power |
| `shelly.power_sign` | `1` | `1` or `-1` to correct sign |
| `controller.poll_interval` | `1` | Seconds between control cycles |
| `controller.deadband_w` | `50` | Ignore grid fluctuations below this (W) |
| `controller.min_soc` | `15` | Never discharge below this (%) |
| `controller.max_soc` | `95` | Never charge above this (%) |
| `controller.watchdog_cycles` | `10` | Stop batteries after this many consecutive unavailable readings |
| `controller.remote_control_interval` | `300` | Re-send remote control enable every N seconds |
| `controller.modbus_scan_interval` | `5` | How often HA polls Modbus sensors (seconds) |

---

## Adding a third battery

Uncomment the third battery block in `config.yaml`, fill in its IP, then re-run `deploy.py` and re-deploy the generated files to HA.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

---

## Key Modbus registers (Venus E v1/v2)

Register map sourced from [ViperRNMC/marstek_venus_modbus](https://github.com/ViperRNMC/marstek_venus_modbus).

| Register | Purpose | R/W |
|----------|---------|-----|
| 32104 | Battery SoC (%) | R |
| 42000 | RS485 remote control — write `21930` to enable | W |
| 43000 | Work mode — write `0` for manual | W |
| 42010 | Force mode: `0`=stop `1`=charge `2`=discharge | W |
| 42020 | Charge power setpoint (W, 0–2500) | W |
| 42021 | Discharge power setpoint (W, 0–2500) | W |
