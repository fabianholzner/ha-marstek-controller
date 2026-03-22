"""
Marstek Venus E multi-battery controller for Home Assistant / AppDaemon.

Reads grid power from Shelly Pro 3EM and distributes charge/discharge
setpoints across N Marstek Venus E batteries to achieve zero grid feed-in.

Power is split proportionally to available SoC capacity so all batteries
stay balanced over time.  Offline batteries are excluded from the split
automatically and reintroduced when their SoC sensor recovers.

Tested register map: Venus E v1/v2 firmware.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import appdaemon.plugins.hass.hassapi as hass

# ---------------------------------------------------------------------------
# Marstek Venus E — Modbus register addresses (v1/v2 firmware)
# Register map reference: https://github.com/ViperRNMC/marstek_venus_modbus
# ---------------------------------------------------------------------------
REG_RS485_CONTROL   = 42000  # 21930=remote ON, 21947=remote OFF
REG_WORK_MODE       = 43000  # 0=manual, 1=anti_feed, 2=trade
REG_FORCE_MODE      = 42010  # 0=stop, 1=charge, 2=discharge
REG_CHARGE_POWER    = 42020  # W, 0-2500, steps of 50
REG_DISCHARGE_POWER = 42021  # W, 0-2500, steps of 50
REG_SOC             = 32104  # %, uint16, scale 1

RS485_REMOTE_ON  = 21930   # 0x55AA — enables Modbus power commands
RS485_REMOTE_OFF = 21947   # 0x55BB

WORK_MODE_MANUAL = 0

FORCE_STOP      = 0
FORCE_CHARGE    = 1
FORCE_DISCHARGE = 2

MAX_POWER_W  = 2500
POWER_STEP_W = 50

INVERTER_CHARGING    = 2
INVERTER_DISCHARGING = 3
VERIFY_THRESHOLD     = 3   # re-init hub after this many consecutive mismatch cycles


# ---------------------------------------------------------------------------
# Per-battery state
# ---------------------------------------------------------------------------

class _Battery:
    """Config and runtime state for one battery."""

    def __init__(self, hub: str, soc_entity: str, slave: int = 1) -> None:
        self.hub        = hub
        self.soc_entity = soc_entity
        self.slave      = slave
        # runtime
        self.soc          = 0.0
        self.available    = True
        self.soc_misses   = 0
        self.last_cmd: str | None = None
        self.cmd_mismatch = 0


# ---------------------------------------------------------------------------
# Pure functions — no AppDaemon dependency, easy to unit-test
# ---------------------------------------------------------------------------

def split_power(total_w: float, socs: list[float],
                min_soc: float, max_soc: float) -> list[float]:
    """
    Split total_w across N batteries weighted by available capacity.

    Charging  (total_w < 0): weight by headroom  = max_soc - SoC
    Discharging (total_w > 0): weight by reserve = SoC   - min_soc

    Returns list of W values (same length as socs).
    Positive = discharge, negative = charge.
    """
    if total_w > 0:
        caps = [max(0.0, s - min_soc) for s in socs]
    else:
        caps = [max(0.0, max_soc - s) for s in socs]

    total_cap = sum(caps)
    if total_cap == 0:
        return [0.0] * len(socs)

    return [total_w * (c / total_cap) for c in caps]


def clamp_to_step(power_w: float, max_power: float = MAX_POWER_W,
                  step: float = POWER_STEP_W) -> int:
    """
    Clamp absolute value to [0, max_power] and round to nearest step.
    Preserves sign. Returns int (Modbus register value).
    """
    sign = 1 if power_w >= 0 else -1
    clamped = min(abs(power_w), max_power)
    stepped = round(clamped / step) * step
    return int(sign * stepped)


# ---------------------------------------------------------------------------
# AppDaemon app
# ---------------------------------------------------------------------------

class BatteryController(hass.Hass):
    """
    AppDaemon app — supports 1 or more Marstek Venus E batteries.

    Configuration (apps.yaml):
        battery_controller:
          module: battery_controller
          class: BatteryController
          grid_power_entity: sensor.shelly_pro3em_XXXXX_total_active_power
          grid_power_sign: 1
          batteries:
            - hub: marstek_1
              soc_entity: sensor.marstek_1_soc
              slave: 1
            - hub: marstek_2
              soc_entity: sensor.marstek_2_soc
              slave: 1
          poll_interval: 1
          deadband_w: 50
          min_soc: 15
          max_soc: 95
          watchdog_cycles: 10
          remote_control_interval: 300
    """

    def initialize(self) -> None:
        self.cfg = self.args

        self._min_soc  = float(self.cfg.get("min_soc",    15))
        self._max_soc  = float(self.cfg.get("max_soc",    95))
        self._deadband = float(self.cfg.get("deadband_w", 50))
        self._poll_s   = int(self.cfg.get("poll_interval", 1))
        self._sign     = float(self.cfg.get("grid_power_sign", 1))
        self._grid_entity = self.cfg["grid_power_entity"]

        self._batteries: list[_Battery] = [
            _Battery(
                hub=b["hub"],
                soc_entity=b["soc_entity"],
                slave=b.get("slave", 1),
            )
            for b in self.cfg.get("batteries", [])
        ]
        if not self._batteries:
            raise ValueError("No batteries configured — add at least one entry under 'batteries:'")

        self._slave = {bat.hub: bat.slave for bat in self._batteries}

        self._watchdog_cycles = int(self.cfg.get("watchdog_cycles", 10))
        self._watchdog_misses = 0
        self._reinit_interval = int(self.cfg.get("remote_control_interval", 300))
        self._cycle_lock      = threading.Lock()

        self._enable_remote_control()
        self.run_every(self._control_loop, "now+5", self._poll_s)
        self.run_every(self._reinit_loop, f"now+{self._reinit_interval}",
                       self._reinit_interval)
        self.log(f"BatteryController started — {len(self._batteries)} batter"
                 f"{'y' if len(self._batteries) == 1 else 'ies'}")

    # ------------------------------------------------------------------
    # Scheduled callbacks
    # ------------------------------------------------------------------

    def _reinit_loop(self, _kwargs: dict) -> None:
        self.log("Periodic remote control re-init", level="DEBUG")
        self._enable_remote_control()

    def _control_loop(self, _kwargs: dict) -> None:
        if not self._cycle_lock.acquire(blocking=False):
            self.log("Previous cycle still running — skipping", level="WARNING")
            return
        try:
            self._run_control()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Control loop error: {exc}", level="ERROR")
        finally:
            self._cycle_lock.release()

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    def _run_control(self) -> None:
        for bat in self._batteries:
            self._verify_battery(bat)

        grid_w = self._read_grid_power()
        if grid_w is None:
            self._watchdog_misses += 1
            if self._watchdog_misses >= self._watchdog_cycles:
                self.log(
                    f"Grid unavailable for {self._watchdog_misses} cycles — "
                    f"stopping all batteries (watchdog)",
                    level="WARNING",
                )
                self._stop_all()
            else:
                self.log(
                    f"Grid power unavailable "
                    f"({self._watchdog_misses}/{self._watchdog_cycles}) — skipping",
                    level="WARNING",
                )
            return

        self._watchdog_misses = 0

        # Read SoC for each battery and update availability.
        # Only batteries with a fresh SoC reading this cycle join the split.
        available = []
        for bat in self._batteries:
            soc = self._read_float(bat.soc_entity)
            if soc is None:
                bat.soc_misses += 1
                if bat.soc_misses >= self._watchdog_cycles:
                    if bat.available:
                        self.log(
                            f"{bat.hub}: SoC unavailable for {bat.soc_misses} "
                            f"cycles — marking offline",
                            level="WARNING",
                        )
                        bat.available = False
                    self._stop(bat.hub)
                # No fresh reading — exclude from this cycle's split
            else:
                if not bat.available:
                    self.log(f"{bat.hub}: SoC restored — back online", level="INFO")
                bat.available  = True
                bat.soc_misses = 0
                bat.soc        = soc
                available.append(bat)
        if not available:
            self.log("All batteries offline — no control possible", level="ERROR")
            return

        status = " | ".join(
            f"{bat.hub}: {'OFFLINE' if not bat.available else f'SoC {bat.soc:.0f}%'}"
            for bat in self._batteries
        )
        self.log(f"Grid: {grid_w:+.0f} W | {status}", level="INFO")

        if abs(grid_w) < self._deadband:
            self.log("Within deadband — stopping batteries", level="DEBUG")
            self._stop_all()
            return

        raw    = split_power(grid_w, [b.soc for b in available],
                             self._min_soc, self._max_soc)
        powers = [clamp_to_step(p) for p in raw]

        log_str = " | ".join(f"{b.hub}: {p:+d} W"
                             for b, p in zip(available, powers))
        self.log(f"Setpoints -> {log_str}")

        with ThreadPoolExecutor(max_workers=len(available)) as ex:
            for bat, p in zip(available, powers):
                ex.submit(self._apply, bat, p)

    # ------------------------------------------------------------------
    # Per-battery setpoint application
    # ------------------------------------------------------------------

    def _apply(self, bat: _Battery, power_w: int) -> None:
        """Apply a signed power setpoint. Positive=discharge, negative=charge."""
        if power_w > 0:
            if bat.soc <= self._min_soc:
                self.log(
                    f"{bat.hub}: SoC {bat.soc:.0f}% <= min {self._min_soc:.0f}% — stopping",
                    level="WARNING",
                )
                self._stop(bat.hub)
                bat.last_cmd = "stop"
            else:
                self._set_discharge(bat.hub, power_w)
                bat.last_cmd = "discharge"
        elif power_w < 0:
            if bat.soc >= self._max_soc:
                self.log(
                    f"{bat.hub}: SoC {bat.soc:.0f}% >= max {self._max_soc:.0f}% — stopping",
                    level="WARNING",
                )
                self._stop(bat.hub)
                bat.last_cmd = "stop"
            else:
                self._set_charge(bat.hub, abs(power_w))
                bat.last_cmd = "charge"
        else:
            self._stop(bat.hub)
            bat.last_cmd = "stop"

    def _stop(self, hub: str) -> None:
        self._write(hub, REG_FORCE_MODE, FORCE_STOP)

    def _stop_all(self) -> None:
        with ThreadPoolExecutor(max_workers=len(self._batteries)) as ex:
            for bat in self._batteries:
                ex.submit(self._stop, bat.hub)
        for bat in self._batteries:
            bat.last_cmd = "stop"

    def _set_charge(self, hub: str, power_w: int) -> None:
        self._write(hub, REG_CHARGE_POWER, power_w)
        self._write(hub, REG_FORCE_MODE, FORCE_CHARGE)

    def _set_discharge(self, hub: str, power_w: int) -> None:
        self._write(hub, REG_DISCHARGE_POWER, power_w)
        self._write(hub, REG_FORCE_MODE, FORCE_DISCHARGE)

    # ------------------------------------------------------------------
    # Remote-control initialisation
    # ------------------------------------------------------------------

    def _enable_remote_control(self) -> None:
        for bat in self._batteries:
            self._write(bat.hub, REG_RS485_CONTROL, RS485_REMOTE_ON)
            self._write(bat.hub, REG_WORK_MODE, WORK_MODE_MANUAL)
            self.log(f"{bat.hub}: remote control enabled")

    # ------------------------------------------------------------------
    # Open-loop verification
    # ------------------------------------------------------------------

    def _verify_battery(self, bat: _Battery) -> None:
        """
        Compare the last commanded mode against the inverter state sensor.
        If they disagree for VERIFY_THRESHOLD consecutive cycles, the battery
        likely lost remote control — re-enable it immediately.
        """
        if bat.last_cmd is None or not bat.available:
            return

        state = self._read_float(f"sensor.{bat.hub}_inverter_state")
        if state is None:
            return

        state = int(state)
        mismatch = (
            (bat.last_cmd == "charge"    and state != INVERTER_CHARGING)
            or (bat.last_cmd == "discharge" and state != INVERTER_DISCHARGING)
        )

        if mismatch:
            bat.cmd_mismatch += 1
            if bat.cmd_mismatch >= VERIFY_THRESHOLD:
                self.log(
                    f"{bat.hub}: inverter state {state} != commanded "
                    f"'{bat.last_cmd}' for {bat.cmd_mismatch} cycles "
                    f"— re-enabling remote control",
                    level="WARNING",
                )
                self._write(bat.hub, REG_RS485_CONTROL, RS485_REMOTE_ON)
                self._write(bat.hub, REG_WORK_MODE, WORK_MODE_MANUAL)
                bat.cmd_mismatch = 0
        else:
            bat.cmd_mismatch = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write(self, hub: str, address: int, value: int) -> None:
        self.log(f"  -> {hub} reg={address} val={value}", level="DEBUG")
        try:
            self.call_service(
                "modbus/write_register",
                hub=hub,
                slave=self._slave[hub],
                address=address,
                value=value,
            )
        except Exception as exc:
            self.log(f"Modbus write failed ({hub} reg={address}): {exc}", level="WARNING")

    def _read_grid_power(self) -> float | None:
        raw = self._read_float(self._grid_entity)
        if raw is None:
            return None
        return raw * self._sign

    def _read_float(self, entity_id: str) -> float | None:
        state = self.get_state(entity_id)
        if state in (None, "unavailable", "unknown"):
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            return None
