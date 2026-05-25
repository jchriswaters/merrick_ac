"""
hvac_sim.py — Python simulation of hvac_controller.ino (runZoneLogic)
=====================================================================
Faithfully ports the MCU control logic so it can be exercised without
hardware.  A MockClock lets tests jump forward in time to trigger
compressor interlocks and dehumidifier timeouts deterministically.

Usage
-----
    python hvac_sim.py          # run all tests, print pass/fail summary
    python hvac_sim.py -v       # verbose: print relay state for every scenario

Control rules covered (from docs/control-logic.md)
---------------------------------------------------
  Rule  1  – mode override: no compressor, fan + dampers + vent
  Rule  2  – heat pump low stage (tempRisingFast True)
  Rule  3  – heat pump high stage (tempRisingFast False)
  Rule  4  – aux electric heat when auxHeatNeeded
  Rule  5  – normal cooling: low stage
  Rule  6  – cooling + high humidity: force high stage
  Rule  7  – dehumidifier-only (no main call, humid, not timed out)
  Rule  8  – dehum timed out: switch to high_cool
  Rule  9  – dehumidifier / compressor mutual exclusion
  Rule 10  – zone dampers default open; close on conflicting call
  Rule 11  – theater zone gated by theaterEnabled config
  Rule 12  – vent blocked by outdoor humidity
  Rule 13  – vent open for free cooling (ventOk)
  Rule 14  – idle: fan-only circulation
  Rule 15  – simultaneous heat+cool call: heat wins
  Rule 16  – compressor interlock: 3-min anti-short-cycle
  Rule 17  – compressor interlock: mode-reversal guard
"""

from __future__ import annotations
import sys
import copy
import textwrap

# ─────────────────────────────────────────────────────────────
# Mock clock (replaces Arduino millis())
# ─────────────────────────────────────────────────────────────

class MockClock:
    def __init__(self):
        self._ms: int = 0

    def millis(self) -> int:
        return self._ms

    def advance_ms(self, ms: int):
        self._ms += ms

    def advance_s(self, s: float):
        self._ms += int(s * 1000)

    def advance_min(self, minutes: float):
        self._ms += int(minutes * 60_000)


# ─────────────────────────────────────────────────────────────
# Data structures (mirror C++ structs)
# ─────────────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self.theater_enabled    = False
        self.vent_min_per_hour  = 10
        self.mode_override      = False
        self.heat_pump_min_temp = 40   # °F
        self.free_cool_max_temp = 60   # °F
        self.high_humidity_pct  = 80
        self.dehum_max_minutes  = 20


class SensorFlags:
    def __init__(self):
        self.heat_pump_ok     = True   # outdoor >= heatPumpMinTempF
        self.aux_heat_needed  = False  # outdoor < heatPumpMinTempF
        self.temp_rising_fast = True   # indoor rising fast → stay on low stage
        self.vent_ok          = False  # free-cooling conditions
        self.vent_blocked     = False  # outdoor humidity too high


class Inputs:
    def __init__(self):
        self.main_low_cool   = False   # D2  Y1
        self.main_high_cool  = False   # D3  Y2
        self.main_heat       = False   # D4  W
        self.theater_cool    = False   # D5
        self.theater_heat    = False   # D6
        self.down_cool       = False   # D7
        self.down_heat       = False   # D8
        self.high_humidity   = False   # D9
        self.vent_in         = False   # D10


class Outputs:
    def __init__(self):
        self.high_cool       = False   # D11 Y2 compressor high stage
        self.low_cool        = False   # D12 Y1 compressor low stage
        self.high_heat       = False   # D13 aux electric
        self.rev_valve       = False   # D14 B-type: True=heating, False=cooling
        self.theater_damper  = False   # D15
        self.down_damper     = False   # D16
        self.vent_out        = False   # D17
        self.dehum_on        = False   # D18
        self.fan_on          = False   # D19 G wire

    def copy(self):
        return copy.copy(self)

    def __repr__(self):
        on = []
        if self.high_cool:      on.append("HIGH_COOL")
        if self.low_cool:       on.append("LOW_COOL")
        if self.high_heat:      on.append("HIGH_HEAT")
        if self.rev_valve:      on.append("REV_VALVE")
        if self.fan_on:         on.append("FAN")
        if self.theater_damper: on.append("THEATER_DAMPR")
        if self.down_damper:    on.append("DOWN_DAMPR")
        if self.vent_out:       on.append("VENT")
        if self.dehum_on:       on.append("DEHUM")
        return "{" + ", ".join(on) + "}" if on else "{all off}"


# ─────────────────────────────────────────────────────────────
# Controller — exact Python port of runZoneLogic()
# ─────────────────────────────────────────────────────────────

INTERLOCK_MS = 180_000   # 3 minutes

class Controller:
    """
    Stateful simulation of the MCU.  Call `tick()` each iteration.
    The caller sets .inp, .sf, and .cfg before each tick.
    After tick() returns, read .out for the resulting relay states.
    """

    def __init__(self, clock: MockClock):
        self._clock               = clock
        self.cfg                  = Config()
        self.sf                   = SensorFlags()
        self.inp                  = Inputs()
        self.out                  = Outputs()

        # Compressor interlock — pre-expire so first boot can start immediately
        self._last_comp_off_ms    = clock.millis() - INTERLOCK_MS
        # Dehumidifier runtime timer
        self._dehum_start_ms      = 0
        self._dehum_timed_out     = False
        # Vent hour timer
        self._vent_hour_start_ms  = clock.millis()

    # ── public helpers ────────────────────────────────────────

    def interlock_active(self) -> bool:
        return (self._clock.millis() - self._last_comp_off_ms) < INTERLOCK_MS

    def check_vent_timer(self) -> bool:
        now = self._clock.millis()
        if (now - self._vent_hour_start_ms) >= 3_600_000:
            self._vent_hour_start_ms = now
        open_ms = self.cfg.vent_min_per_hour * 60_000
        return (now - self._vent_hour_start_ms) < open_ms

    # ── main logic tick ───────────────────────────────────────

    def tick(self):
        """Run one control cycle (equivalent to one Arduino loop() call)."""
        inp = self.inp
        sf  = self.sf
        cfg = self.cfg
        out = self.out          # current (previous) state
        now = self._clock.millis()

        desired = Outputs()

        # ── Clear dehum timeout when humidity finally drops ──────
        if not inp.high_humidity:
            self._dehum_timed_out = False

        # ── Check dehumidifier timeout while running ─────────────
        if out.dehum_on and not self._dehum_timed_out:
            max_ms = cfg.dehum_max_minutes * 60_000
            if (now - self._dehum_start_ms) >= max_ms:
                self._dehum_timed_out = True

        # ── MODE OVERRIDE ────────────────────────────────────────
        if cfg.mode_override:
            desired.fan_on        = True
            desired.theater_damper = True
            desired.down_damper   = True
            if sf.vent_blocked:
                desired.vent_out = False
            elif sf.vent_ok:
                desired.vent_out = True
            else:
                desired.vent_out = inp.vent_in or self.check_vent_timer()
            self.out = desired
            return

        # ── Resolve simultaneous heat+cool: heat wins ────────────
        main_cool_call = inp.main_low_cool or inp.main_high_cool
        main_heat_call = inp.main_heat
        if main_heat_call and main_cool_call:
            main_cool_call = False

        # ════════════════════════════════════════════════════════
        # COMPRESSOR / HEAT LOGIC
        # ════════════════════════════════════════════════════════

        if main_heat_call:
            # Rules 2–4: heat pump heating, optional aux electric
            desired.rev_valve = True   # B-type: energise for heating
            desired.fan_on    = True

            if sf.temp_rising_fast:
                desired.low_cool  = True   # rule 2: low stage
            else:
                desired.high_cool = True   # rule 3: high stage

            if sf.aux_heat_needed:
                desired.high_heat = True   # rule 4: aux electric

        elif main_cool_call:
            # Rules 5–6: cooling
            desired.rev_valve = False
            desired.fan_on    = True

            if inp.high_humidity:
                desired.high_cool = True   # rule 6: high stage for moisture removal
            else:
                desired.low_cool  = True   # rule 5: normal low stage

        else:
            # No main thermostat call
            if inp.high_humidity:
                if self._dehum_timed_out:
                    # Rule 8: switch to high_cool until humidity clears
                    desired.high_cool = True
                    desired.fan_on    = True
                else:
                    # Rule 7: dehumidifier + fan; no compressor
                    desired.dehum_on = True
                    desired.fan_on   = True
            else:
                # Rule 14: idle — fan-only
                desired.fan_on = True

        # ── Track dehumidifier start time ────────────────────────
        if desired.dehum_on and not out.dehum_on:
            self._dehum_start_ms = now

        # ── Rule 9: dehumidifier / compressor mutual exclusion ───
        if desired.dehum_on:
            desired.high_cool = False
            desired.low_cool  = False
            desired.fan_on    = True

        # Compressor stages always imply fan
        if desired.high_cool or desired.low_cool or desired.high_heat:
            desired.fan_on = True

        # ════════════════════════════════════════════════════════
        # COMPRESSOR INTERLOCK (anti-short-cycle + mode-reversal)
        # ════════════════════════════════════════════════════════

        was_comp_on   = out.high_cool or out.low_cool
        wants_comp_on = desired.high_cool or desired.low_cool

        # Mode-reversal guard: if valve must change while compressor is running,
        # force compressor off immediately and hold the valve.  The normal
        # anti-short-cycle block below will protect the restart in the new mode.
        if was_comp_on and wants_comp_on and (desired.rev_valve != out.rev_valve):
            desired.high_cool = False
            desired.low_cool  = False
            desired.high_heat = False
            desired.rev_valve = out.rev_valve   # hold valve
            wants_comp_on     = False           # let timer start below

        # Start interlock timer when compressor turns off
        if was_comp_on and not wants_comp_on:
            self._last_comp_off_ms = now

        # Block compressor start during lockout (anti-short-cycle)
        if not was_comp_on and wants_comp_on and self.interlock_active():
            desired.high_cool = False
            desired.low_cool  = False
            desired.high_heat = False
            desired.rev_valve = out.rev_valve     # hold valve
            desired.fan_on    = out.fan_on        # hold fan

        # ════════════════════════════════════════════════════════
        # ZONE DAMPER LOGIC (rules 10–11)
        # Default OPEN; close only on conflicting zone call.
        # ════════════════════════════════════════════════════════

        main_is_heating = desired.rev_valve and (desired.low_cool or desired.high_cool)
        main_is_cooling = (not desired.rev_valve) and (desired.low_cool or desired.high_cool)

        # Theater damper (gated by theaterEnabled)
        if cfg.theater_enabled:
            conflict = ((inp.theater_heat and main_is_cooling) or
                        (inp.theater_cool and main_is_heating))
            desired.theater_damper = not conflict
        else:
            desired.theater_damper = True   # zone disabled: always open

        # Downstairs damper
        conflict = ((inp.down_heat and main_is_cooling) or
                    (inp.down_cool and main_is_heating))
        desired.down_damper = not conflict

        # ════════════════════════════════════════════════════════
        # VENT LOGIC (rules 12–13 + timer)
        # ════════════════════════════════════════════════════════

        if sf.vent_blocked:
            desired.vent_out = False
        elif sf.vent_ok:
            desired.vent_out = True
        else:
            desired.vent_out = inp.vent_in or self.check_vent_timer()

        # ── Commit ───────────────────────────────────────────────
        self.out = desired


# ─────────────────────────────────────────────────────────────
# Test framework helpers
# ─────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

_results: list[tuple[str, bool, str, Outputs]] = []


def scenario(
    name: str,
    ctrl: Controller,
    expect: dict,
    ticks: int = 3,
    verbose: bool = False,
) -> bool:
    """
    Run `ticks` control cycles then check every key in `expect` against
    the resulting Outputs.  Returns True on full pass.

    expect keys map directly to Outputs attributes (snake_case).
    """
    for _ in range(ticks):
        ctrl.tick()

    out = ctrl.out
    failures = []
    for attr, expected_val in expect.items():
        actual = getattr(out, attr)
        if actual != expected_val:
            failures.append(f"{attr}: expected {expected_val}, got {actual}")

    passed = len(failures) == 0
    detail = "; ".join(failures) if failures else ""
    _results.append((name, passed, detail, out.copy()))

    if verbose:
        status = PASS if passed else FAIL
        print(f"  {status}  {name}")
        print(f"         outputs: {out}")
        if failures:
            for f in failures:
                print(f"         !! {f}")
    return passed


def fresh(clock: MockClock = None) -> tuple[Controller, MockClock]:
    """Return a brand-new Controller + clock pair."""
    if clock is None:
        clock = MockClock()
    return Controller(clock), clock


def print_summary():
    total  = len(_results)
    passed = sum(1 for _, ok, _, _ in _results if ok)
    failed = total - passed

    print()
    print("=" * 68)
    print(f" HVAC Controller Simulation — {total} scenarios")
    print("=" * 68)
    for name, ok, detail, out in _results:
        status = PASS if ok else FAIL
        print(f"  {status}  {name}")
        if not ok:
            print(f"         {detail}")
            print(f"         actual outputs: {out}")
    print("=" * 68)
    print(f" {passed} passed  |  {failed} failed")
    print("=" * 68)
    print()
    return failed


# ═════════════════════════════════════════════════════════════
# TEST SCENARIOS
# ═════════════════════════════════════════════════════════════

def run_all(verbose: bool = False):

    print()
    print("Running HVAC controller logic simulation …")
    print()

    # ─────────────────────────────────────────────
    # GROUP 1 — Idle / no call
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    # All inputs false, normal sensor flags (defaults)
    scenario("Rule 14 – idle: fan-only, no compressor", ctrl,
             dict(fan_on=True, high_cool=False, low_cool=False,
                  high_heat=False, dehum_on=False),
             verbose=verbose)

    ctrl, _ = fresh()
    # Idle → dampers default open
    scenario("Rule 14 – idle: both dampers open by default", ctrl,
             dict(theater_damper=True, down_damper=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 2 — Heat pump heating
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.main_heat       = True
    ctrl.sf.temp_rising_fast = True    # heat pump keeping up → low stage
    scenario("Rule 2 – heat: low stage when tempRisingFast=True", ctrl,
             dict(low_cool=True, high_cool=False, rev_valve=True, fan_on=True, high_heat=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_heat       = True
    ctrl.sf.temp_rising_fast = False   # not keeping up → high stage
    scenario("Rule 3 – heat: high stage when tempRisingFast=False", ctrl,
             dict(high_cool=True, low_cool=False, rev_valve=True, fan_on=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_heat       = True
    ctrl.sf.temp_rising_fast = True
    ctrl.sf.aux_heat_needed  = True
    scenario("Rule 4 – heat + auxHeatNeeded: heat pump + aux electric together", ctrl,
             dict(low_cool=True, high_heat=True, rev_valve=True, fan_on=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_heat       = True
    ctrl.sf.temp_rising_fast = False
    ctrl.sf.aux_heat_needed  = True
    scenario("Rule 4 – heat high stage + auxHeatNeeded", ctrl,
             dict(high_cool=True, high_heat=True, rev_valve=True, fan_on=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 3 — Cooling
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool = True
    scenario("Rule 5 – cool normal: low stage, no humidity", ctrl,
             dict(low_cool=True, high_cool=False, rev_valve=False, fan_on=True, dehum_on=False),
             verbose=verbose)

    # Y2 from the thermostat is treated as "cooling needed" — stage is decided
    # by the MCU based on humidity (rule 6), not the thermostat's Y1/Y2 request.
    # When Y2 is called alone and humidity is normal, the MCU runs low stage.
    ctrl, _ = fresh()
    ctrl.inp.main_high_cool = True
    scenario("Rule 5 – cool Y2 direct call (no humidity): MCU selects low stage", ctrl,
             dict(high_cool=False, low_cool=True, rev_valve=False, fan_on=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool  = True
    ctrl.inp.high_humidity  = True
    scenario("Rule 6 – cool + high humidity: force high stage (moisture removal)", ctrl,
             dict(high_cool=True, low_cool=False, rev_valve=False, fan_on=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 4 — Dehumidification (no main call)
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.high_humidity = True
    scenario("Rule 7 – dehum: dehumidifier + fan, no compressor", ctrl,
             dict(dehum_on=True, fan_on=True, high_cool=False, low_cool=False),
             verbose=verbose)

    # Dehum timer timeout — advance past dehumMaxMinutes
    ctrl, clock = fresh()
    ctrl.inp.high_humidity = True
    ctrl.tick()                        # start dehumidifier (dehum_start_ms set)
    clock.advance_min(21)              # past default 20-minute limit
    ctrl.tick()                        # should detect timeout
    scenario("Rule 8 – dehum timed out: switch to high_cool", ctrl,
             dict(dehum_on=False, high_cool=True, fan_on=True),
             ticks=0,                  # state already advanced; just check
             verbose=verbose)

    # Dehum timer resets when humidity clears
    ctrl, clock = fresh()
    ctrl.inp.high_humidity = True
    ctrl.tick()
    clock.advance_min(21)
    ctrl.tick()                        # dehumTimedOut = True
    ctrl.inp.high_humidity = False     # humidity clears
    ctrl.tick()                        # should reset timeout
    ctrl.inp.high_humidity = True      # humidity returns
    scenario("Rule 8 – dehum timeout resets when humidity clears", ctrl,
             dict(dehum_on=True, high_cool=False),
             ticks=1,
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 5 — Rule 9: dehumidifier/compressor mutual exclusion
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.high_humidity = True   # would normally → dehumOn
    # Even if somehow desired.dehumOn were set, compressor outputs must be cleared.
    # Verify: dehumOn=True forces highCool=False, lowCool=False
    scenario("Rule 9 – dehum on: compressor outputs forced off", ctrl,
             dict(dehum_on=True, high_cool=False, low_cool=False, fan_on=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool  = True   # cooling call → dehumOn must NOT be set
    ctrl.inp.high_humidity  = True   # humidity high but cooling call takes priority
    scenario("Rule 9 – cooling call with humidity: compressor on, no dehumidifier", ctrl,
             dict(dehum_on=False, high_cool=True, low_cool=False),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 6 — Compressor interlock (anti-short-cycle)
    # ─────────────────────────────────────────────

    ctrl, clock = fresh()
    # Get compressor running (cooling)
    ctrl.inp.main_low_cool = True
    ctrl.tick()
    assert ctrl.out.low_cool, "Setup: compressor should be on"
    # Turn off cooling call → compressor should stop and start interlock
    ctrl.inp.main_low_cool = False
    ctrl.tick()
    assert not ctrl.out.low_cool, "Setup: compressor should be off"
    # Immediately try to start again
    ctrl.inp.main_low_cool = True
    scenario("Rule 16 – interlock: compressor start blocked within 3 min", ctrl,
             dict(low_cool=False, high_cool=False),
             ticks=1,
             verbose=verbose)

    ctrl, clock = fresh()
    ctrl.inp.main_low_cool = True
    ctrl.tick()
    ctrl.inp.main_low_cool = False
    ctrl.tick()
    clock.advance_min(3.1)             # past 3-minute lockout
    ctrl.inp.main_low_cool = True
    scenario("Rule 16 – interlock: compressor start allowed after 3 min", ctrl,
             dict(low_cool=True),
             ticks=1,
             verbose=verbose)

    # Fan and dehumidifier may still run during interlock
    ctrl, clock = fresh()
    ctrl.inp.main_low_cool = True
    ctrl.tick()
    ctrl.inp.main_low_cool = False
    ctrl.tick()
    ctrl.inp.high_humidity = True      # humidity rises during lockout
    scenario("Rule 16 – interlock: dehumidifier can run during lockout", ctrl,
             dict(dehum_on=True, high_cool=False, low_cool=False),
             ticks=1,
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 7 — Mode-reversal guard
    # ─────────────────────────────────────────────

    ctrl, clock = fresh()
    # Compressor running in cooling mode
    ctrl.inp.main_low_cool = True
    ctrl.tick()
    assert ctrl.out.low_cool and not ctrl.out.rev_valve, "Setup: cooling"
    # While still running, try to switch to heating (rev_valve should be blocked)
    ctrl.inp.main_low_cool = False
    ctrl.inp.main_heat     = True
    # Immediately: compressor off → interlock starts → reversal blocked
    scenario("Rule 17 – mode reversal blocked during interlock", ctrl,
             dict(low_cool=False, high_cool=False, rev_valve=False),
             ticks=1,
             verbose=verbose)

    ctrl, clock = fresh()
    ctrl.inp.main_low_cool = True
    ctrl.tick()                        # tick 1: cooling starts
    ctrl.inp.main_low_cool = False
    ctrl.inp.main_heat     = True
    ctrl.tick()                        # tick 2: mode reversal detected; compressor
                                       #   forced off NOW, interlock timer starts here
    clock.advance_min(3.1)             # advance clock past 3-min lockout
    scenario("Rule 17 – mode reversal allowed after interlock expires", ctrl,
             dict(rev_valve=True, low_cool=True),
             ticks=1,                  # tick 3: interlock expired; heating starts
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 8 — Zone dampers
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.cfg.theater_enabled = False
    ctrl.inp.main_heat       = True
    ctrl.inp.theater_cool    = True    # conflict, but theater disabled
    scenario("Rule 11 – theater disabled: damper always open despite conflict", ctrl,
             dict(theater_damper=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.theater_enabled = True
    ctrl.inp.main_low_cool   = True    # main = cooling
    ctrl.inp.theater_heat    = True    # theater wants heat → conflict
    scenario("Rule 10/11 – theater heat vs main cooling: theater damper CLOSED", ctrl,
             dict(theater_damper=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.theater_enabled = True
    ctrl.inp.main_heat       = True    # main = heating
    ctrl.inp.theater_cool    = True    # theater wants cool → conflict
    scenario("Rule 10/11 – theater cool vs main heating: theater damper CLOSED", ctrl,
             dict(theater_damper=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.theater_enabled = True
    ctrl.inp.main_heat       = True
    ctrl.inp.theater_heat    = True    # same mode → no conflict
    scenario("Rule 10/11 – theater heat vs main heating: damper OPEN", ctrl,
             dict(theater_damper=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.theater_enabled = True
    ctrl.inp.main_heat       = True
    # theater not calling at all
    scenario("Rule 11 – theater satisfied (no call) while main heats: damper OPEN", ctrl,
             dict(theater_damper=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool   = True    # main = cooling
    ctrl.inp.down_heat       = True    # downstairs wants heat → conflict
    scenario("Rule 10 – downstairs heat vs main cooling: down damper CLOSED", ctrl,
             dict(down_damper=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool   = True
    ctrl.inp.down_cool       = True    # same mode → no conflict
    scenario("Rule 10 – downstairs cool vs main cooling: down damper OPEN", ctrl,
             dict(down_damper=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool   = True
    # downstairs not calling
    scenario("Rule 10 – downstairs satisfied (no call): down damper OPEN", ctrl,
             dict(down_damper=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 9 — Vent logic
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.sf.vent_blocked = True
    ctrl.sf.vent_ok      = True    # ventOk would open, but ventBlocked wins
    scenario("Rule 12 – ventBlocked overrides ventOk: vent CLOSED", ctrl,
             dict(vent_out=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.sf.vent_blocked = False
    ctrl.sf.vent_ok      = True
    scenario("Rule 13 – ventOk: vent OPEN for free cooling", ctrl,
             dict(vent_out=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.sf.vent_blocked = False
    ctrl.sf.vent_ok      = False
    ctrl.inp.vent_in     = True    # hardware input
    scenario("Vent hardware input (ventIn=True): vent OPEN", ctrl,
             dict(vent_out=True),
             verbose=verbose)

    # Vent timer: at t=0, vent should be open for first ventMinPerHour minutes
    ctrl, clock = fresh()
    ctrl.cfg.vent_min_per_hour = 10
    ctrl.sf.vent_blocked = False
    ctrl.sf.vent_ok      = False
    scenario("Vent timer: vent OPEN at start of hour window", ctrl,
             dict(vent_out=True),
             verbose=verbose)

    # After ventMinPerHour minutes, vent should close
    ctrl, clock = fresh()
    ctrl.cfg.vent_min_per_hour = 10
    ctrl.sf.vent_blocked = False
    ctrl.sf.vent_ok      = False
    clock.advance_min(11)    # past 10-minute vent window
    scenario("Vent timer: vent CLOSED after window expires", ctrl,
             dict(vent_out=False),
             verbose=verbose)

    # ventMinPerHour=0 → vent never opens from timer
    ctrl, clock = fresh()
    ctrl.cfg.vent_min_per_hour = 0
    ctrl.sf.vent_blocked = False
    ctrl.sf.vent_ok      = False
    scenario("Vent timer: vent_min_per_hour=0 -> vent stays CLOSED", ctrl,
             dict(vent_out=False),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 10 — Mode override
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.cfg.mode_override  = True
    ctrl.inp.main_heat      = True    # heat call active but override beats it
    scenario("Rule 1 – mode override: no compressor, fan on", ctrl,
             dict(fan_on=True, high_cool=False, low_cool=False,
                  high_heat=False, dehum_on=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.mode_override  = True
    scenario("Rule 1 – mode override: both dampers forced open", ctrl,
             dict(theater_damper=True, down_damper=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.mode_override  = True
    ctrl.sf.vent_blocked    = True
    scenario("Rule 1 – mode override + ventBlocked: vent stays closed", ctrl,
             dict(vent_out=False),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.cfg.mode_override  = True
    ctrl.sf.vent_ok         = True
    scenario("Rule 1 – mode override + ventOk: vent still opens", ctrl,
             dict(vent_out=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 11 — Priority / edge cases
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.main_heat      = True
    ctrl.inp.main_low_cool  = True    # simultaneous heat + cool → heat wins
    scenario("Rule 15 – simultaneous heat+cool call: heat wins", ctrl,
             dict(rev_valve=True, low_cool=True, high_cool=False, fan_on=True),
             verbose=verbose)

    # Heating — auxHeatNeeded=True but heatPumpOk=False is informational only;
    # heat pump still runs (no separate "disable heat pump" path)
    ctrl, _ = fresh()
    ctrl.inp.main_heat       = True
    ctrl.sf.heat_pump_ok     = False   # informational; not used in MCU logic
    ctrl.sf.aux_heat_needed  = True
    ctrl.sf.temp_rising_fast = True
    scenario("heatPumpOk=False is informational: heat pump still runs + aux", ctrl,
             dict(low_cool=True, high_heat=True, rev_valve=True),
             verbose=verbose)

    # Y1+Y2 together: stage is still decided by humidity, not thermostat request.
    # Without humidity, the MCU selects low stage regardless of Y2 being asserted.
    ctrl, _ = fresh()
    ctrl.inp.main_high_cool = True
    ctrl.inp.main_low_cool  = True    # both Y1 and Y2 called
    scenario("Cool: Y1+Y2 both called, no humidity -> MCU selects low stage", ctrl,
             dict(high_cool=False, low_cool=True),
             verbose=verbose)

    # Y1+Y2 together WITH high humidity: MCU escalates to high stage (rule 6)
    ctrl, _ = fresh()
    ctrl.inp.main_high_cool = True
    ctrl.inp.main_low_cool  = True
    ctrl.inp.high_humidity  = True
    scenario("Cool: Y1+Y2 + high humidity -> high stage (rule 6)", ctrl,
             dict(high_cool=True, low_cool=False),
             verbose=verbose)

    # revValve must be FALSE (cooling) when cooling call is active
    ctrl, _ = fresh()
    ctrl.inp.main_low_cool = True
    scenario("Reversing valve: FALSE (cooling) during cool call", ctrl,
             dict(rev_valve=False),
             verbose=verbose)

    # revValve must be TRUE (heating) when heat call is active
    ctrl, _ = fresh()
    ctrl.inp.main_heat = True
    scenario("Reversing valve: TRUE (heating) during heat call", ctrl,
             dict(rev_valve=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 12 — Fan always implied by compressor
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.main_heat = True
    scenario("Fan implied: fan_on=True whenever compressor is running (heat)", ctrl,
             dict(fan_on=True),
             verbose=verbose)

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool = True
    scenario("Fan implied: fan_on=True whenever compressor is running (cool)", ctrl,
             dict(fan_on=True),
             verbose=verbose)

    # ─────────────────────────────────────────────
    # GROUP 13 — Startup safety (power-on interlock pre-expired)
    # ─────────────────────────────────────────────

    ctrl, _ = fresh()
    ctrl.inp.main_low_cool = True
    scenario("Startup: compressor starts immediately (interlock pre-expired)", ctrl,
             dict(low_cool=True),
             ticks=1,
             verbose=verbose)

    return print_summary()


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    failed = run_all(verbose=verbose)
    sys.exit(1 if failed else 0)
