"""
Tests for battery_controller.py

Pure functions (split_power, clamp_to_step) are tested directly.
The AppDaemon BatteryController class is tested via a lightweight mock
that replaces the hass.Hass parent with a plain object so no AppDaemon
daemon infrastructure is needed.
"""

import sys
import types
from unittest.mock import MagicMock, call
import pytest

# ---------------------------------------------------------------------------
# Stub out the appdaemon package so the import succeeds without a running
# AppDaemon instance.
# ---------------------------------------------------------------------------

def _make_appdaemon_stub():
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass_pkg = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        """Minimal stub replacing appdaemon.plugins.hass.hassapi.Hass."""
        def __init__(self):
            self.args = {}
        def log(self, *a, **kw): pass
        def get_state(self, entity_id): return None
        def call_service(self, service, **kw): pass
        def run_every(self, cb, when, interval): pass

    hassapi.Hass = Hass
    hass_pkg.hassapi = hassapi
    plugins.hass = hass_pkg
    ad.plugins = plugins

    sys.modules.setdefault("appdaemon", ad)
    sys.modules.setdefault("appdaemon.plugins", plugins)
    sys.modules.setdefault("appdaemon.plugins.hass", hass_pkg)
    sys.modules.setdefault("appdaemon.plugins.hass.hassapi", hassapi)


_make_appdaemon_stub()

# Now safe to import
from battery_controller import (  # noqa: E402
    _Battery,
    BatteryController,
    clamp_to_step,
    split_power,
    FORCE_CHARGE,
    FORCE_DISCHARGE,
    FORCE_STOP,
    INVERTER_CHARGING,
    INVERTER_DISCHARGING,
    MAX_POWER_W,
    POWER_STEP_W,
    REG_CHARGE_POWER,
    REG_DISCHARGE_POWER,
    REG_FORCE_MODE,
    REG_RS485_CONTROL,
    REG_WORK_MODE,
    RS485_REMOTE_ON,
    VERIFY_THRESHOLD,
    WORK_MODE_MANUAL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_CFG = {
    "grid_power_entity": "sensor.grid_power",
    "grid_power_sign":   1,
    "batteries": [
        {"hub": "marstek_1", "soc_entity": "sensor.soc_1", "slave": 1},
        {"hub": "marstek_2", "soc_entity": "sensor.soc_2", "slave": 1},
    ],
    "poll_interval":          1,
    "deadband_w":             50,
    "min_soc":                15,
    "max_soc":                95,
    "watchdog_cycles":        10,
    "remote_control_interval": 300,
}


def make_controller(cfg_overrides: dict | None = None) -> BatteryController:
    """Create a BatteryController without triggering AppDaemon initialize."""
    ctrl = BatteryController.__new__(BatteryController)
    ctrl.args = {**DEFAULT_CFG, **(cfg_overrides or {})}
    ctrl.log = MagicMock()
    ctrl.call_service = MagicMock()
    ctrl.run_every = MagicMock()
    ctrl.get_state = MagicMock(return_value=None)
    ctrl.initialize()
    return ctrl


def make_battery(hub: str = "marstek_1", soc: float = 60.0,
                 slave: int = 1) -> _Battery:
    bat = _Battery(hub=hub, soc_entity=f"sensor.{hub}_soc", slave=slave)
    bat.soc = soc
    return bat


# ---------------------------------------------------------------------------
# split_power
# ---------------------------------------------------------------------------

class TestSplitPower:
    def test_equal_soc_splits_evenly(self):
        p1, p2 = split_power(1000, [50, 50], min_soc=15, max_soc=95)
        assert p1 == pytest.approx(500)
        assert p2 == pytest.approx(500)

    def test_discharge_weighted_by_soc(self):
        p1, p2 = split_power(1000, [80, 40], min_soc=15, max_soc=95)
        # cap1=65, cap2=25, total=90
        assert p1 == pytest.approx(1000 * 65 / 90)
        assert p2 == pytest.approx(1000 * 25 / 90)
        assert p1 > p2

    def test_charge_weighted_by_headroom(self):
        p1, p2 = split_power(-1000, [80, 40], min_soc=15, max_soc=95)
        # cap1=15, cap2=55, total=70
        assert p1 == pytest.approx(-1000 * 15 / 70)
        assert p2 == pytest.approx(-1000 * 55 / 70)
        assert abs(p2) > abs(p1)

    def test_both_at_min_soc_returns_zero(self):
        p1, p2 = split_power(500, [15, 15], min_soc=15, max_soc=95)
        assert p1 == 0
        assert p2 == 0

    def test_both_at_max_soc_returns_zero(self):
        p1, p2 = split_power(-500, [95, 95], min_soc=15, max_soc=95)
        assert p1 == 0
        assert p2 == 0

    def test_one_battery_at_limit_gets_zero(self):
        p1, p2 = split_power(500, [15, 60], min_soc=15, max_soc=95)
        assert p1 == 0
        assert p2 == pytest.approx(500)

    def test_three_batteries_equal_soc(self):
        p1, p2, p3 = split_power(900, [50, 50, 50], min_soc=15, max_soc=95)
        assert p1 == pytest.approx(300)
        assert p2 == pytest.approx(300)
        assert p3 == pytest.approx(300)

    def test_three_batteries_weighted(self):
        # caps: 65, 45, 25 → total 135
        p1, p2, p3 = split_power(1350, [80, 60, 40], min_soc=15, max_soc=95)
        assert p1 == pytest.approx(1350 * 65 / 135)
        assert p2 == pytest.approx(1350 * 45 / 135)
        assert p3 == pytest.approx(1350 * 25 / 135)


# ---------------------------------------------------------------------------
# clamp_to_step
# ---------------------------------------------------------------------------

class TestClampToStep:
    def test_rounds_to_nearest_50(self):
        assert clamp_to_step(275) == 300
        assert clamp_to_step(224) == 200

    def test_preserves_sign(self):
        assert clamp_to_step(-300) == -300
        assert clamp_to_step(-275) == -300

    def test_clamps_at_max(self):
        assert clamp_to_step(9999) == MAX_POWER_W
        assert clamp_to_step(-9999) == -MAX_POWER_W

    def test_zero(self):
        assert clamp_to_step(0) == 0

    def test_exact_step_unchanged(self):
        assert clamp_to_step(2500) == 2500
        assert clamp_to_step(50) == 50


# ---------------------------------------------------------------------------
# BatteryController._apply
# ---------------------------------------------------------------------------

class TestApply:
    def setup_method(self):
        self.ctrl = make_controller()
        self.ctrl._write = MagicMock()

    def test_discharge_writes_correct_registers(self):
        bat = make_battery("marstek_1", soc=60)
        self.ctrl._apply(bat, 500)
        self.ctrl._write.assert_any_call("marstek_1", REG_DISCHARGE_POWER, 500)
        self.ctrl._write.assert_any_call("marstek_1", REG_FORCE_MODE, FORCE_DISCHARGE)
        assert bat.last_cmd == "discharge"

    def test_charge_writes_correct_registers(self):
        bat = make_battery("marstek_1", soc=50)
        self.ctrl._apply(bat, -400)
        self.ctrl._write.assert_any_call("marstek_1", REG_CHARGE_POWER, 400)
        self.ctrl._write.assert_any_call("marstek_1", REG_FORCE_MODE, FORCE_CHARGE)
        assert bat.last_cmd == "charge"

    def test_zero_power_stops_battery(self):
        bat = make_battery("marstek_1", soc=50)
        self.ctrl._apply(bat, 0)
        self.ctrl._write.assert_called_with("marstek_1", REG_FORCE_MODE, FORCE_STOP)
        assert bat.last_cmd == "stop"

    def test_discharge_blocked_at_min_soc(self):
        bat = make_battery("marstek_1", soc=15)
        self.ctrl._apply(bat, 500)
        self.ctrl._write.assert_called_with("marstek_1", REG_FORCE_MODE, FORCE_STOP)
        assert bat.last_cmd == "stop"

    def test_charge_blocked_at_max_soc(self):
        bat = make_battery("marstek_1", soc=95)
        self.ctrl._apply(bat, -400)
        self.ctrl._write.assert_called_with("marstek_1", REG_FORCE_MODE, FORCE_STOP)
        assert bat.last_cmd == "stop"


# ---------------------------------------------------------------------------
# BatteryController._run_control — integration-level
# ---------------------------------------------------------------------------

class TestRunControl:
    def _make(self, grid, soc1, soc2):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        states = {
            "sensor.grid_power": str(grid),
            "sensor.soc_1": str(soc1),
            "sensor.soc_2": str(soc2),
        }
        ctrl.get_state = lambda e: states.get(e)
        return ctrl

    def test_exports_split_between_batteries(self):
        ctrl = self._make(grid=-1000, soc1=50, soc2=50)
        ctrl._run_control()
        calls = [c for c in ctrl._write.call_args_list
                 if c == call("marstek_1", REG_FORCE_MODE, FORCE_CHARGE)
                 or c == call("marstek_2", REG_FORCE_MODE, FORCE_CHARGE)]
        assert len(calls) == 2

    def test_imports_discharge_batteries(self):
        ctrl = self._make(grid=800, soc1=60, soc2=60)
        ctrl._run_control()
        calls = [c for c in ctrl._write.call_args_list
                 if c == call("marstek_1", REG_FORCE_MODE, FORCE_DISCHARGE)
                 or c == call("marstek_2", REG_FORCE_MODE, FORCE_DISCHARGE)]
        assert len(calls) == 2

    def test_deadband_stops_both_batteries(self):
        ctrl = self._make(grid=30, soc1=50, soc2=50)
        ctrl._run_control()
        stop_calls = [c for c in ctrl._write.call_args_list
                      if c.args[2] == FORCE_STOP]
        assert len(stop_calls) == 2

    def test_unavailable_grid_first_miss_skips_without_stop(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        ctrl.get_state = lambda e: "unavailable"
        ctrl._run_control()
        ctrl._write.assert_not_called()

    def test_watchdog_stops_batteries_after_threshold(self):
        ctrl = make_controller({"watchdog_cycles": 2})
        ctrl._write = MagicMock()
        ctrl.get_state = lambda e: "unavailable"
        ctrl._run_control()  # miss 1
        ctrl._write.assert_not_called()
        ctrl._run_control()  # miss 2 -> stop
        stop_calls = [c for c in ctrl._write.call_args_list if c.args[2] == FORCE_STOP]
        assert len(stop_calls) == 2

    def test_soc_watchdog_stops_batteries_after_threshold(self):
        ctrl = make_controller({"watchdog_cycles": 2})
        ctrl._write = MagicMock()
        ctrl.get_state = lambda e: "500" if "grid" in e else "unavailable"
        ctrl._run_control()  # miss 1
        ctrl._write.assert_not_called()
        ctrl._run_control()  # miss 2 -> stop
        stop_calls = [c for c in ctrl._write.call_args_list if c.args[2] == FORCE_STOP]
        assert len(stop_calls) == 2

    def test_watchdog_resets_after_recovery(self):
        ctrl = make_controller({"watchdog_cycles": 2})
        ctrl._write = MagicMock()
        states = {"sensor.grid_power": "unavailable",
                  "sensor.soc_1": "60", "sensor.soc_2": "60"}
        ctrl.get_state = lambda e: states.get(e)
        ctrl._run_control()
        assert ctrl._watchdog_misses == 1
        states["sensor.grid_power"] = "500"
        ctrl._run_control()
        assert ctrl._watchdog_misses == 0

    def test_negative_sign_config(self):
        ctrl = make_controller({"grid_power_sign": -1})
        ctrl._write = MagicMock()
        ctrl.get_state = lambda e: "-800" if "grid" in e else "60"
        ctrl._run_control()
        discharge_calls = [c for c in ctrl._write.call_args_list
                           if c.args[2] == FORCE_DISCHARGE]
        assert len(discharge_calls) == 2


# ---------------------------------------------------------------------------
# Auto-detect: one battery goes offline
# ---------------------------------------------------------------------------

class TestAutoDetect:
    def test_offline_battery_excluded_from_split(self):
        """When one battery's SoC is unavailable long enough, all power goes to the other."""
        ctrl = make_controller({"watchdog_cycles": 2})
        ctrl._write = MagicMock()
        # soc_2 unavailable; soc_1 fine; grid importing 500W
        states = {"sensor.grid_power": "500",
                  "sensor.soc_1": "60",
                  "sensor.soc_2": "unavailable"}
        ctrl.get_state = lambda e: states.get(e)

        ctrl._run_control()  # miss 1 for bat2 — still included (but skipped this cycle)
        ctrl._run_control()  # miss 2 — bat2 marked offline

        # After going offline, marstek_2 should be stopped and marstek_1 gets full power
        ctrl._write.reset_mock()
        ctrl._run_control()

        discharge_1 = [c for c in ctrl._write.call_args_list
                       if c == call("marstek_1", REG_FORCE_MODE, FORCE_DISCHARGE)]
        assert len(discharge_1) == 1

    def test_battery_comes_back_online(self):
        """After recovery, the battery is reintroduced."""
        ctrl = make_controller({"watchdog_cycles": 2})
        ctrl._write = MagicMock()
        states = {"sensor.grid_power": "500",
                  "sensor.soc_1": "60",
                  "sensor.soc_2": "unavailable"}
        ctrl.get_state = lambda e: states.get(e)

        ctrl._run_control()
        ctrl._run_control()
        assert not ctrl._batteries[1].available

        # Battery 2 comes back
        states["sensor.soc_2"] = "40"
        ctrl._run_control()
        assert ctrl._batteries[1].available

    def test_three_battery_one_offline(self):
        """With 3 batteries, offline one splits power across remaining two."""
        cfg = {**DEFAULT_CFG, "watchdog_cycles": 1,
               "batteries": [
                   {"hub": "m1", "soc_entity": "sensor.soc_1", "slave": 1},
                   {"hub": "m2", "soc_entity": "sensor.soc_2", "slave": 1},
                   {"hub": "m3", "soc_entity": "sensor.soc_3", "slave": 1},
               ]}
        ctrl = make_controller(cfg)
        ctrl._write = MagicMock()
        states = {"sensor.grid_power": "600",
                  "sensor.soc_1": "60", "sensor.soc_2": "unavailable",
                  "sensor.soc_3": "60"}
        ctrl.get_state = lambda e: states.get(e)

        ctrl._run_control()  # m2 miss 1 -> offline (watchdog_cycles=1)

        ctrl._write.reset_mock()
        ctrl._run_control()

        discharge_m1 = [c for c in ctrl._write.call_args_list
                        if c == call("m1", REG_FORCE_MODE, FORCE_DISCHARGE)]
        discharge_m3 = [c for c in ctrl._write.call_args_list
                        if c == call("m3", REG_FORCE_MODE, FORCE_DISCHARGE)]
        assert len(discharge_m1) == 1
        assert len(discharge_m3) == 1


# ---------------------------------------------------------------------------
# Slave ID
# ---------------------------------------------------------------------------

class TestSlaveId:
    def test_slave_id_from_config(self):
        cfg = {**DEFAULT_CFG, "batteries": [
            {"hub": "marstek_1", "soc_entity": "sensor.soc_1", "slave": 3},
            {"hub": "marstek_2", "soc_entity": "sensor.soc_2", "slave": 5},
        ]}
        ctrl = make_controller(cfg)
        calls = []
        ctrl.call_service = lambda svc, **kw: calls.append(kw)
        ctrl._write("marstek_1", REG_FORCE_MODE, FORCE_STOP)
        ctrl._write("marstek_2", REG_FORCE_MODE, FORCE_STOP)
        assert calls[0]["slave"] == 3
        assert calls[1]["slave"] == 5


# ---------------------------------------------------------------------------
# Open-loop verification
# ---------------------------------------------------------------------------

class TestVerifyBattery:
    def test_matching_state_clears_counter(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        bat = ctrl._batteries[0]
        bat.last_cmd = "charge"
        bat.cmd_mismatch = 2
        ctrl.get_state = lambda e: str(INVERTER_CHARGING) if bat.hub in e else None
        ctrl._verify_battery(bat)
        assert bat.cmd_mismatch == 0
        ctrl._write.assert_not_called()

    def test_mismatch_increments_counter(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        bat = ctrl._batteries[0]
        bat.last_cmd = "charge"
        ctrl.get_state = lambda e: str(INVERTER_DISCHARGING) if bat.hub in e else None
        ctrl._verify_battery(bat)
        assert bat.cmd_mismatch == 1
        ctrl._write.assert_not_called()

    def test_mismatch_at_threshold_triggers_reinit(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        bat = ctrl._batteries[0]
        bat.last_cmd = "discharge"
        bat.cmd_mismatch = VERIFY_THRESHOLD - 1
        ctrl.get_state = lambda e: str(INVERTER_CHARGING) if bat.hub in e else None
        ctrl._verify_battery(bat)
        ctrl._write.assert_any_call(bat.hub, REG_RS485_CONTROL, RS485_REMOTE_ON)
        ctrl._write.assert_any_call(bat.hub, REG_WORK_MODE, WORK_MODE_MANUAL)
        assert bat.cmd_mismatch == 0

    def test_unavailable_sensor_skips(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        bat = ctrl._batteries[0]
        bat.last_cmd = "charge"
        ctrl.get_state = lambda e: None
        ctrl._verify_battery(bat)
        ctrl._write.assert_not_called()


# ---------------------------------------------------------------------------
# Periodic re-init
# ---------------------------------------------------------------------------

class TestPeriodicReinit:
    def test_reinit_loop_enables_all_batteries(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        ctrl._reinit_loop({})
        for bat in ctrl._batteries:
            ctrl._write.assert_any_call(bat.hub, REG_RS485_CONTROL, RS485_REMOTE_ON)
            ctrl._write.assert_any_call(bat.hub, REG_WORK_MODE, WORK_MODE_MANUAL)


# ---------------------------------------------------------------------------
# Cycle lock
# ---------------------------------------------------------------------------

class TestCycleLock:
    def test_concurrent_cycle_is_skipped(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        ctrl.get_state = lambda e: "500" if "grid" in e else "60"
        ctrl._cycle_lock.acquire()
        ctrl._control_loop({})
        ctrl._write.assert_not_called()
        ctrl._cycle_lock.release()


# ---------------------------------------------------------------------------
# Remote control initialisation
# ---------------------------------------------------------------------------

class TestEnableRemoteControl:
    def test_writes_rs485_and_work_mode_on_all_hubs(self):
        ctrl = make_controller()
        ctrl._write = MagicMock()
        ctrl._enable_remote_control()
        for bat in ctrl._batteries:
            ctrl._write.assert_any_call(bat.hub, REG_RS485_CONTROL, RS485_REMOTE_ON)
            ctrl._write.assert_any_call(bat.hub, REG_WORK_MODE, WORK_MODE_MANUAL)
