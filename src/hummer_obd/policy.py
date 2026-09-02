"""How often to ask the vehicle, and when to stop asking it at all.

The collector's original rule was one line: a cycle that got data waits
``poll_interval_s``, a cycle that got nothing waits ``idle_backoff_s``.  That
cannot tell apart the vehicle states this project has actually measured, and
one of the readings it lumps into "got nothing" is not a vehicle state at all
-- it is a fault.

This module is that decision and nothing else.  It takes an
:class:`Observation`, which is what the last cycle saw, and returns a
:class:`Decision`, which is the next interval, whether OBD requests are allowed
at all, and whether to stop.  It performs no I/O, opens no port, and imports
neither ``transport`` nor ``session``, so every transition below is reachable
from a unit test by constructing an ``Observation``, with no fake adapter in
the way.  That testability is the only reason this is a separate module: a
state machine buried inside a polling loop is a state machine whose edges are
exercised by luck.

The refusal at the centre of it
------------------------------
``NO DATA`` means one of two entirely different things, and the adapter's CAN
error counters are what separate them.  ``docs/VALIDATION.md`` records the
measurement: with the protocol forced to ``ATSP7``, ``0100`` returned ``NO
DATA`` while ``ATCS`` reported ``T:00 R:00`` -- zero transmit errors and zero
receive errors.  The request went out correctly and nothing answered, which is
a sleeping vehicle.  ``NO DATA`` with *non-zero* counters is the opposite
finding: the frame never made it onto the bus, which is wiring, the connector,
or the adapter.

A fault must therefore never be absorbed into ASLEEP.  Doing so would be the
most comfortable possible bug: the node would back off to one request every
five minutes, the recorded state would read "asleep", and a broken pin would be
indistinguishable from a truck sitting still -- for as long as nobody thought
to look.  :meth:`Policy.decide` leaves the state, the interval and all progress
towards sleep completely untouched when ``can_status_clean`` is ``False``, and
does the same when it is ``None``, because an ``ATCS`` reply that could not be
parsed is not evidence either.

RECENTLY_PARKED and NOT_SERVING share an interval but not a meaning
------------------------------------------------------------------
Nobody answering (``NO DATA`` on a provably healthy bus) and a module answering
with a refusal (``7F <service> 22`` from ``28``, the brake system controller,
which stays reachable longest during shutdown) are different observations about
the vehicle.  They are kept as different states because the state is recorded,
and collapsing them would throw away the distinction the validation record went
to some trouble to establish.

Two batteries
-------------
``Observation.volts`` is the **vehicle's** 12 V rail, read from pin 16 of the
J1962 connector by ``ATRV`` (see ``voltage.py``).  ``Observation.battery_low``
comes from ``battery.py``, which watches the **PiSugar cell** that powers the
Pi.  Two different batteries, two different chemistries, two different
thresholds, about nine volts apart.  Comparing one against the other's
threshold would produce a node that either never stops or stops on its first
cycle, so they stay separate fields and share no constant.

``battery_low`` is acted on the moment it arrives, because ``battery.py`` has
already required a run of consecutive low readings before raising it.  The 12 V
rail arrives unfiltered, so it gets its own streak here.

The two voltage thresholds are the least-proven numbers in this file
--------------------------------------------------------------------
``wake_volts = 13.0`` and ``low_volts = 12.2`` are reasoned from a very small
record.  ``docs/VALIDATION.md`` caught one transition directly -- 13.9 V with
the DC-DC converter running, then 12.7 V five minutes later with the vehicle
asleep -- but the summary table there (and the identical one in
``docs/CAPABILITIES.md``) does **not** record a single asleep value.  It
records a band:

===============================  ==================
vehicle state                    ``ATRV``
===============================  ==================
awake, DC-DC converter running   13.9 V
powered off, bus silent          12.7 V -- 13.0 V
===============================  ==================

A ``wake_volts`` of 13.0 would sit exactly **on** the top of that asleep band,
and the comparison is ``>=``, so a 13.0 V reading -- a value the validation
record itself lists as "powered off, bus silent" -- would wake the policy out
of ASLEEP and re-enable OBD requests on a truck that is merely parked.  That is
the wrong direction to be wrong in, so the default is **13.4 V**.

13.4 is not a guess dressed up as a number: it is bounded on both sides by the
two readings that exist.  It is strictly above the highest observed resting
value (13.0) and strictly below the observed running value (13.9), so it cannot
mistake rest for running, and the failure it *can* still have is the harmless
one -- staying asleep a little longer than necessary on a vehicle that has
genuinely woken, which costs some data and sends no traffic.

It is still not calibrated.  The honest version of this number comes from
trending ``ATRV`` across a full sleep period -- the measurement
``docs/VALIDATION.md`` says the collector gate is waiting on -- and setting
``wake_volts`` above the highest resting value that shows.

``low_volts = 12.2`` sits below the whole recorded band and is the safer of the
two, but it is no better calibrated.  Neither threshold has been checked
against a full sleep period.  They are configuration, not constants, for that
reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Final, Optional

__all__ = [
    "DRIVING",
    "AWAKE",
    "RECENTLY_PARKED",
    "NOT_SERVING",
    "ASLEEP",
    "LOW_BATTERY",
    "STATES",
    "Observation",
    "Decision",
    "PolicyConfig",
    "Policy",
]

#: Data is flowing and the vehicle is moving.  The only state worth a fast poll.
DRIVING: Final[str] = "DRIVING"
#: Data is flowing with the vehicle stationary: accessory, charging, or idling.
AWAKE: Final[str] = "AWAKE"
#: Nobody answered, on a bus the adapter proved it could transmit on.
RECENTLY_PARKED: Final[str] = "RECENTLY_PARKED"
#: A module answered with ``conditionsNotCorrect``: alive, and declining.
NOT_SERVING: Final[str] = "NOT_SERVING"
#: Confirmed quiet.  No OBD request is permitted in this state.
ASLEEP: Final[str] = "ASLEEP"
#: The node's own power, or the vehicle's 12 V rail, is too low to keep going.
LOW_BATTERY: Final[str] = "LOW_BATTERY"

STATES: Final[tuple[str, ...]] = (
    DRIVING, AWAKE, RECENTLY_PARKED, NOT_SERVING, ASLEEP, LOW_BATTERY)

#: The two states that share the parked interval.  Moving between them keeps
#: the sleep window running: both mean "not serving data", and only the reason
#: differs, so a vehicle that goes from refusing to silent has not restarted
#: its shutdown.
_QUIET_STATES: Final[frozenset[str]] = frozenset({RECENTLY_PARKED, NOT_SERVING})

#: States in which nothing may be put on the bus.  ASLEEP is the point of the
#: whole module; LOW_BATTERY is here because a decision that says "stop" must
#: not simultaneously say "but requests are fine".
_NO_OBD_STATES: Final[frozenset[str]] = frozenset({ASLEEP, LOW_BATTERY})


@dataclass(frozen=True)
class Observation:
    """What one collector cycle saw.

    Every field defaults to "no evidence", which is what makes an incompletely
    filled observation safe: it holds the current state rather than advancing
    towards sleep.  A caller that cannot determine a field should leave it
    alone rather than guess it.
    """

    #: At least one module returned a positive response this cycle.
    had_data: bool = False
    #: Speed (PID ``0D``) is above zero.
    moving: bool = False
    #: Any ``(service, 0x22)`` negative response -- ``conditionsNotCorrect``.
    conditions_not_correct: bool = False
    #: The adapter reported ``NO DATA`` for the requests that were sent.
    no_data: bool = False
    #: ``ATCS T:00 R:00`` -> ``True``; non-zero counters -> ``False``; a reply
    #: that could not be parsed, or no reply at all -> ``None``.  ``None`` and
    #: ``False`` are both refusals to conclude anything, for different reasons.
    can_status_clean: Optional[bool] = None
    #: ``ATRV``: the **vehicle's** 12 V rail, not the Pi's battery.
    volts: Optional[float] = None
    #: The PiSugar cell, as judged by ``battery.py``.
    battery_low: bool = False


@dataclass(frozen=True)
class Decision:
    """What to do next, and the recorded reason for it."""

    state: str
    interval_s: float
    obd_allowed: bool
    stop: bool
    reason: str


@dataclass
class PolicyConfig:
    """Intervals, windows and thresholds, all of them overridable.

    The defaults are the ones this vehicle's behaviour suggests; see the module
    docstring for how little evidence stands behind the two voltages.
    """

    drive_interval_s: float = 2.0
    awake_interval_s: float = 5.0
    parked_interval_s: float = 45.0
    #: How long a vehicle may stay in a non-serving state before it is called
    #: asleep, whatever the cycle count says.  Fifteen minutes is comfortably
    #: longer than the roughly five minutes this truck took to shut its DC-DC
    #: converter down after parking.
    parked_window_s: float = 900.0
    asleep_interval_s: float = 300.0
    #: Consecutive silent-with-clean-counters cycles required before sleep is
    #: declared.  More than one, because a single missed cycle is normal.
    asleep_confirm_cycles: int = 3
    #: Above the observed asleep band (12.7-13.0 V) and below the observed
    #: running value (13.9 V), so a resting reading can never be mistaken for
    #: a running one.  See the module docstring; calibrate against a full
    #: sleep period before relying on it.
    wake_volts: float = 13.4
    low_volts: float = 12.2
    #: Consecutive low readings on the vehicle's 12 V rail before stopping.
    #: Same reasoning as ``battery.py``: one low reading proves nothing.
    low_volts_consecutive: int = 3
    #: An operator floor on the returned interval.  It can only ever *raise*
    #: an interval, so an override is incapable of making the node poll harder
    #: than its own state machine decided.  ``0`` means no floor.
    floor_interval_s: float = 0.0

    def validate(self) -> None:
        """Raise ``ValueError`` on a configuration that cannot be honoured."""
        positive = (
            ("drive_interval_s", self.drive_interval_s),
            ("awake_interval_s", self.awake_interval_s),
            ("parked_interval_s", self.parked_interval_s),
            ("parked_window_s", self.parked_window_s),
            ("asleep_interval_s", self.asleep_interval_s),
            ("wake_volts", self.wake_volts),
            ("low_volts", self.low_volts),
        )
        for name, value in positive:
            if value <= 0:
                raise ValueError(f"policy.{name} must be greater than zero")
        for name, count in (("asleep_confirm_cycles", self.asleep_confirm_cycles),
                            ("low_volts_consecutive", self.low_volts_consecutive)):
            if count < 1:
                raise ValueError(f"policy.{name} must be at least 1")
        if self.floor_interval_s < 0:
            raise ValueError(
                "policy.floor_interval_s must not be negative; 0 means no floor")
        if not (self.drive_interval_s <= self.awake_interval_s
                <= self.parked_interval_s <= self.asleep_interval_s):
            # A quieter vehicle must never be polled harder than a busy one.
            raise ValueError(
                "policy intervals must not decrease as the vehicle goes quieter: "
                "drive <= awake <= parked <= asleep, got "
                f"{self.drive_interval_s:g} <= {self.awake_interval_s:g} <= "
                f"{self.parked_interval_s:g} <= {self.asleep_interval_s:g}")
        if self.wake_volts <= self.low_volts:
            # Otherwise one reading could be both "the vehicle woke up" and
            # "stop, the battery is flat", and which one won would be an
            # accident of the order the checks happen to be written in.
            raise ValueError(
                f"policy.wake_volts ({self.wake_volts:g}) must be above "
                f"policy.low_volts ({self.low_volts:g})")

    def interval_for(self, state: str) -> float:
        """The interval for *state*, with the operator floor applied.

        The floor is applied here, in the one place every decision passes
        through, so that no branch added later can forget it.
        """
        base = {
            DRIVING: self.drive_interval_s,
            AWAKE: self.awake_interval_s,
            RECENTLY_PARKED: self.parked_interval_s,
            NOT_SERVING: self.parked_interval_s,
            ASLEEP: self.asleep_interval_s,
            # Stopping does not need an interval, but a supervisor that ignored
            # ``stop`` would get the gentlest one rather than the fastest.
            LOW_BATTERY: self.asleep_interval_s,
        }.get(state)
        if base is None:
            raise ValueError(f"unknown policy state {state!r}")
        return max(base, self.floor_interval_s)


class Policy:
    """The state machine.  Feed it observations; it returns decisions.

    Precedence, highest first, because several rules can match one
    observation and the order is the design:

    1. **Battery.**  Either battery being low stops the node.  This outranks
       everything, including the fault hold below: a CAN fault is not evidence
       that the batteries are fine, and stopping is the conservative act.
       The 12 V streak keeps accumulating during a bus fault on purpose --
       ``ATRV`` reads a pin inside the adapter and never touches the bus, so a
       broken bus does not make the voltage reading less true.
    2. **Positive data.**  A module answered, so the vehicle is awake; this
       outranks a ``NO DATA`` in the same cycle, because one module answering
       is proof and silence from another is not.
    3. **A refusal.**  ``conditionsNotCorrect`` means a module is alive, which
       outranks the fault hold: something is clearly on the bus.
    4. **Waking on voltage**, from ASLEEP only.
    5. **Silence**, which advances towards sleep only with clean CAN counters.
    6. **Everything else holds**, changing nothing at all.
    """

    def __init__(self, config: Optional[PolicyConfig] = None, *,
                 state: str = AWAKE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = config or PolicyConfig()
        # Validated at construction: a node must not start polling on a
        # configuration whose intervals contradict each other.
        self.cfg.validate()
        if state not in STATES:
            raise ValueError(f"unknown policy state {state!r}")
        #: The reported state.  AWAKE by default: before the first observation
        #: nothing is known, and the awake interval is the milder of the two
        #: intervals that could be wrong in the direction of extra traffic.
        self.state = state
        #: Consecutive low readings on the vehicle's 12 V rail, values kept so
        #: the recorded reason can quote them.
        self.low_volts_streak: list[float] = []
        #: Consecutive cycles of silence on a provably healthy bus.
        self.asleep_streak: int = 0
        #: When the vehicle entered the quiet group, for ``parked_window_s``.
        self.quiet_since: Optional[float] = None
        #: Where to resume if the battery recovers, so a stop does not erase
        #: what was known about the vehicle.
        self._resume_state: str = AWAKE if state == LOW_BATTERY else state
        self._clock = clock

    # -- decision -----------------------------------------------------------

    def decide(self, obs: Observation, now: Optional[float] = None) -> Decision:
        """Fold *obs* into the state machine and return the next decision.

        *now* is a monotonic seconds value.  It is a parameter rather than a
        call into the clock so that a test can drive a fifteen minute window in
        one line; the injected clock is only the default.
        """
        now = self._clock() if now is None else now

        stop_reason = self._battery_verdict(obs)
        if stop_reason is not None:
            if self.state != LOW_BATTERY:
                self._resume_state = self.state
                self.state = LOW_BATTERY
            return self._decision(LOW_BATTERY, stop_reason)

        recovered = self.state == LOW_BATTERY
        if recovered:
            # Both batteries are back above their thresholds.  Resume from what
            # was last known about the vehicle rather than from an assumption,
            # and let this same observation move it on.
            self.state = self._resume_state

        decision = self._vehicle_decision(obs, now)
        if recovered:
            return replace(decision, reason=f"battery_recovered; {decision.reason}")
        return decision

    def _vehicle_decision(self, obs: Observation, now: float) -> Decision:
        if obs.had_data:
            if obs.moving:
                return self._serving(DRIVING, "data_moving: modules answered and "
                                              "speed is above zero")
            return self._serving(AWAKE, "data_stationary: modules answered and "
                                        "speed is zero")

        if obs.conditions_not_correct:
            # A refusal is an answer.  It is not silence, so it cannot count
            # towards the confirmation streak -- but the vehicle still is not
            # serving data, so the window keeps running.
            self.asleep_streak = 0
            return self._quiet(NOT_SERVING, now,
                               "conditions_not_correct: a module is alive and "
                               "declining to serve diagnostics")

        if (self.state == ASLEEP and obs.volts is not None
                and obs.volts >= self.cfg.wake_volts):
            return self._serving(
                AWAKE,
                f"wake_volts: {obs.volts:.2f} V is at or above "
                f"{self.cfg.wake_volts:g} V, so something is charging the rail")

        if obs.no_data:
            if obs.can_status_clean is True:
                self.asleep_streak += 1
                if self.state == ASLEEP:
                    return self._decision(
                        ASLEEP, "still_asleep: NO DATA with T:00 R:00")
                return self._quiet(RECENTLY_PARKED, now,
                                   "no_data_clean: NO DATA with T:00 R:00, so the "
                                   "request went out and nothing answered")
            if obs.can_status_clean is False:
                # The line this module exists to draw.  Nothing is touched:
                # not the state, not the interval, not the streak, not the
                # window.  A fault that ages into ASLEEP is a fault nobody
                # ever sees.
                return self._hold(
                    "can_fault_hold: NO DATA with non-zero CAN counters is a "
                    "wiring or adapter fault, not sleep")
            return self._hold(
                "can_status_unknown_hold: NO DATA without a readable ATCS reply "
                "proves nothing either way")

        return self._hold("no_signal_hold: nothing was observed this cycle")

    # -- transitions --------------------------------------------------------

    def _serving(self, state: str, reason: str) -> Decision:
        """Enter a state where the vehicle is answering, clearing sleep progress."""
        self.state = state
        self.asleep_streak = 0
        self.quiet_since = None
        return self._decision(state, reason)

    def _quiet(self, state: str, now: float, reason: str) -> Decision:
        """Enter or stay in the quiet group, and check both routes to sleep."""
        if self.state not in _QUIET_STATES or self.quiet_since is None:
            self.quiet_since = now
        self.state = state
        elapsed = now - self.quiet_since
        if self.asleep_streak >= self.cfg.asleep_confirm_cycles:
            self.state = ASLEEP
            return self._decision(
                ASLEEP,
                f"asleep_confirmed: {self.asleep_streak} consecutive silent "
                f"cycles with clean CAN counters")
        if elapsed >= self.cfg.parked_window_s:
            self.state = ASLEEP
            return self._decision(
                ASLEEP,
                f"asleep_window: {elapsed:.0f}s without serving data, past the "
                f"{self.cfg.parked_window_s:g}s window")
        return self._decision(
            state,
            f"{reason} ({self.asleep_streak}/{self.cfg.asleep_confirm_cycles} "
            f"silent cycles, {elapsed:.0f}s quiet)")

    def _hold(self, reason: str) -> Decision:
        """Change nothing.  Every counter and timestamp keeps its value."""
        return self._decision(self.state, f"{reason}; holding {self.state}")

    def _decision(self, state: str, reason: str) -> Decision:
        return Decision(
            state=state,
            interval_s=self.cfg.interval_for(state),
            obd_allowed=state not in _NO_OBD_STATES,
            stop=state == LOW_BATTERY,
            reason=reason,
        )

    # -- power --------------------------------------------------------------

    def _battery_verdict(self, obs: Observation) -> Optional[str]:
        """Return the reason to stop, or ``None`` to keep running.

        The streak is updated first and unconditionally, so a missing reading
        breaks it: a flapping ``ATRV`` must not accumulate towards a stop, the
        same rule ``battery.py`` applies to the cell.
        """
        if obs.volts is None or obs.volts > self.cfg.low_volts:
            self.low_volts_streak.clear()
        else:
            self.low_volts_streak.append(obs.volts)
        if obs.battery_low:
            return ("battery_low: the PiSugar cell is low, and stopping while "
                    "there is charge left is what protects the SD card")
        if len(self.low_volts_streak) >= self.cfg.low_volts_consecutive:
            return (f"low_volts: {len(self.low_volts_streak)} consecutive readings "
                    f"at or below {self.cfg.low_volts:g} V on the vehicle's 12 V "
                    f"rail (last {self.low_volts_streak[-1]:.2f} V)")
        return None
