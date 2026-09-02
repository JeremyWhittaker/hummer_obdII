"""The policy decides how hard to poll, so its refusals matter more than its moves.

Two of these tests exist because the obvious implementation gets them wrong.
``NO DATA`` with non-zero CAN error counters is a wiring or adapter fault, and
folding it into ASLEEP would hide a broken connector behind a state that looks
entirely healthy; ``NO DATA`` with an unparsed ``ATCS`` reply proves nothing at
all.  Neither may move the state or advance a single cycle towards sleep, no
matter how long it goes on for.
"""

import unittest

from hummer_obd.policy import (
    ASLEEP,
    AWAKE,
    DRIVING,
    LOW_BATTERY,
    NOT_SERVING,
    RECENTLY_PARKED,
    STATES,
    Decision,
    Observation,
    Policy,
    PolicyConfig,
)


class Clock:
    """A monotonic clock a test can drive fifteen minutes forward in one line."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


#: The two readings the validation record actually contains.
DCDC_RUNNING = 13.9
ASLEEP_RESTING = 12.7

#: What a cycle sees when the vehicle is answering, when a module is refusing,
#: when the bus is provably silent, and when the adapter cannot transmit.
SERVING = Observation(had_data=True, volts=DCDC_RUNNING)
REFUSING = Observation(conditions_not_correct=True, volts=ASLEEP_RESTING)
SILENT = Observation(no_data=True, can_status_clean=True, volts=ASLEEP_RESTING)
FAULT = Observation(no_data=True, can_status_clean=False, volts=ASLEEP_RESTING)
UNPARSED = Observation(no_data=True, can_status_clean=None, volts=ASLEEP_RESTING)


def token(decision: Decision) -> str:
    """The stable leading token of a recorded reason."""
    return decision.reason.split(":", 1)[0]


def parked_policy(clock, **cfg_kwargs):
    """A policy that has just seen one silent cycle, so it sits in RECENTLY_PARKED."""
    policy = Policy(PolicyConfig(**cfg_kwargs), clock=clock)
    policy.decide(SILENT)
    assert policy.state == RECENTLY_PARKED, policy.state
    return policy


class TestConfigValidation(unittest.TestCase):
    def test_the_defaults_validate(self):
        PolicyConfig().validate()

    def test_every_interval_and_threshold_must_be_positive(self):
        for name in ("drive_interval_s", "awake_interval_s", "parked_interval_s",
                     "parked_window_s", "asleep_interval_s", "wake_volts",
                     "low_volts"):
            for bad in (0.0, -1.0):
                cfg = PolicyConfig(**{name: bad})
                with self.assertRaises(ValueError) as caught:
                    cfg.validate()
                self.assertIn(name, str(caught.exception))

    def test_confirmation_counts_must_be_at_least_one(self):
        for name in ("asleep_confirm_cycles", "low_volts_consecutive"):
            with self.assertRaises(ValueError) as caught:
                PolicyConfig(**{name: 0}).validate()
            self.assertIn(name, str(caught.exception))

    def test_intervals_must_not_decrease_as_the_vehicle_goes_quieter(self):
        # A parked vehicle polled harder than a driving one is the whole thing
        # this project is trying not to do.
        with self.assertRaises(ValueError):
            PolicyConfig(parked_interval_s=1.0).validate()
        with self.assertRaises(ValueError):
            PolicyConfig(drive_interval_s=10.0).validate()
        with self.assertRaises(ValueError):
            PolicyConfig(asleep_interval_s=10.0).validate()

    def test_equal_intervals_are_allowed(self):
        PolicyConfig(drive_interval_s=5.0, awake_interval_s=5.0,
                     parked_interval_s=5.0, asleep_interval_s=5.0).validate()

    def test_the_wake_threshold_must_sit_above_the_stop_threshold(self):
        # Otherwise one reading would mean both "it woke up" and "stop now".
        with self.assertRaises(ValueError):
            PolicyConfig(wake_volts=12.0, low_volts=12.2).validate()
        with self.assertRaises(ValueError):
            PolicyConfig(wake_volts=12.2, low_volts=12.2).validate()

    def test_the_floor_may_be_zero_but_not_negative(self):
        PolicyConfig(floor_interval_s=0.0).validate()
        with self.assertRaises(ValueError):
            PolicyConfig(floor_interval_s=-1.0).validate()

    def test_a_policy_refuses_to_start_on_an_invalid_config(self):
        with self.assertRaises(ValueError):
            Policy(PolicyConfig(drive_interval_s=0.0))

    def test_a_policy_refuses_an_unknown_starting_state(self):
        with self.assertRaises(ValueError):
            Policy(state="PARKED_ISH")

    def test_interval_for_refuses_an_unknown_state(self):
        with self.assertRaises(ValueError):
            PolicyConfig().interval_for("PARKED_ISH")


class TestServingStates(unittest.TestCase):
    def test_data_while_moving_is_driving(self):
        policy = Policy(clock=Clock())
        decision = policy.decide(Observation(had_data=True, moving=True,
                                             volts=DCDC_RUNNING))
        self.assertEqual(decision.state, DRIVING)
        self.assertEqual(decision.interval_s, 2.0)
        self.assertTrue(decision.obd_allowed)
        self.assertFalse(decision.stop)

    def test_data_while_stationary_is_awake(self):
        policy = Policy(clock=Clock())
        decision = policy.decide(SERVING)
        self.assertEqual(decision.state, AWAKE)
        self.assertEqual(decision.interval_s, 5.0)

    def test_data_clears_progress_towards_sleep(self):
        clock = Clock()
        policy = parked_policy(clock)
        self.assertEqual(policy.asleep_streak, 1)
        policy.decide(SERVING)
        self.assertEqual(policy.asleep_streak, 0)
        self.assertIsNone(policy.quiet_since)

    def test_data_outranks_no_data_in_the_same_cycle(self):
        # Some PIDs answered and others returned NO DATA.  One module
        # answering is proof the vehicle is up; silence from another is not.
        policy = Policy(clock=Clock())
        decision = policy.decide(Observation(had_data=True, no_data=True,
                                             can_status_clean=True))
        self.assertEqual(decision.state, AWAKE)
        self.assertEqual(policy.asleep_streak, 0)


class TestQuietStates(unittest.TestCase):
    def test_a_refusal_is_not_serving_not_recently_parked(self):
        policy = Policy(clock=Clock())
        decision = policy.decide(REFUSING)
        self.assertEqual(decision.state, NOT_SERVING)
        self.assertEqual(decision.interval_s, 45.0)
        self.assertTrue(decision.obd_allowed)

    def test_silence_on_a_healthy_bus_is_recently_parked(self):
        policy = Policy(clock=Clock())
        decision = policy.decide(SILENT)
        self.assertEqual(decision.state, RECENTLY_PARKED)
        self.assertEqual(decision.interval_s, 45.0)

    def test_the_two_quiet_states_stay_distinct(self):
        # They share an interval and mean different things, and the state is
        # what gets recorded.
        policy = Policy(clock=Clock())
        self.assertEqual(policy.decide(REFUSING).state, NOT_SERVING)
        self.assertEqual(policy.decide(SILENT).state, RECENTLY_PARKED)
        self.assertNotEqual(NOT_SERVING, RECENTLY_PARKED)

    def test_consecutive_silent_cycles_confirm_sleep(self):
        clock = Clock()
        policy = Policy(clock=clock)
        states = []
        for _ in range(3):
            states.append(policy.decide(SILENT).state)
            clock.advance(45.0)
        self.assertEqual(states, [RECENTLY_PARKED, RECENTLY_PARKED, ASLEEP])

    def test_the_confirmation_count_is_configurable(self):
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        self.assertEqual(policy.decide(SILENT).state, ASLEEP)

    def test_a_refusal_does_not_count_towards_the_silence_streak(self):
        # Module 28 answering is not silence, whatever it answered.
        clock = Clock()
        policy = Policy(clock=clock)
        policy.decide(SILENT)
        policy.decide(SILENT)
        self.assertEqual(policy.asleep_streak, 2)
        policy.decide(REFUSING)
        self.assertEqual(policy.asleep_streak, 0)
        self.assertEqual(policy.state, NOT_SERVING)

    def test_the_parked_window_confirms_sleep_without_a_streak(self):
        clock = Clock()
        policy = Policy(clock=clock)
        self.assertEqual(policy.decide(REFUSING).state, NOT_SERVING)
        clock.advance(900.0)
        decision = policy.decide(REFUSING)
        self.assertEqual(decision.state, ASLEEP)
        self.assertEqual(token(decision), "asleep_window")

    def test_the_window_keeps_running_across_the_two_quiet_states(self):
        # Refusing then falling silent is one shutdown, not two.  Resetting the
        # clock on the change would leave a parked vehicle polled every 45s.
        clock = Clock()
        policy = Policy(clock=clock)
        policy.decide(REFUSING)
        started = policy.quiet_since
        clock.advance(600.0)
        policy.decide(REFUSING)
        self.assertEqual(policy.quiet_since, started)
        clock.advance(300.0)
        decision = policy.decide(SILENT)
        self.assertEqual(decision.state, ASLEEP)

    def test_the_window_restarts_after_the_vehicle_serves_data_again(self):
        clock = Clock()
        policy = Policy(clock=clock)
        policy.decide(REFUSING)
        clock.advance(800.0)
        policy.decide(SERVING)
        clock.advance(200.0)
        decision = policy.decide(REFUSING)
        self.assertEqual(decision.state, NOT_SERVING)
        self.assertEqual(policy.quiet_since, clock.now)

    def test_a_policy_started_in_a_quiet_state_still_gets_a_window(self):
        clock = Clock()
        policy = Policy(state=RECENTLY_PARKED, clock=clock)
        self.assertIsNone(policy.quiet_since)
        policy.decide(SILENT)
        self.assertEqual(policy.quiet_since, clock.now)


class TestAsleep(unittest.TestCase):
    def test_asleep_never_permits_a_request(self):
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        decision = policy.decide(SILENT)
        self.assertEqual(decision.state, ASLEEP)
        self.assertFalse(decision.obd_allowed)
        self.assertEqual(decision.interval_s, 300.0)
        self.assertFalse(decision.stop)

    def test_asleep_stays_asleep_while_the_bus_stays_silent(self):
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        policy.decide(SILENT)
        clock.advance(300.0)
        decision = policy.decide(SILENT)
        self.assertEqual(decision.state, ASLEEP)
        self.assertFalse(decision.obd_allowed)

    def test_an_observation_with_nothing_in_it_leaves_asleep_alone(self):
        # The usual asleep cycle: obd_allowed is False, so nothing was asked
        # and the only reading is ATRV.
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        policy.decide(SILENT)
        decision = policy.decide(Observation(volts=ASLEEP_RESTING))
        self.assertEqual(decision.state, ASLEEP)
        self.assertEqual(token(decision), "no_signal_hold")

    def test_a_charging_rail_wakes_the_policy(self):
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        policy.decide(SILENT)
        decision = policy.decide(Observation(volts=DCDC_RUNNING))
        self.assertEqual(decision.state, AWAKE)
        self.assertEqual(token(decision), "wake_volts")
        self.assertTrue(decision.obd_allowed)

    def test_the_resting_voltage_does_not_wake_the_policy(self):
        # 12.7 V is what this vehicle read while asleep.  Waking on it would
        # make the asleep state unreachable in practice.
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        policy.decide(SILENT)
        self.assertEqual(policy.decide(Observation(volts=ASLEEP_RESTING)).state,
                         ASLEEP)

    def test_data_wakes_the_policy_even_though_asleep_forbids_asking(self):
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        policy.decide(SILENT)
        decision = policy.decide(Observation(had_data=True, moving=True))
        self.assertEqual(decision.state, DRIVING)

    def test_a_refusal_from_asleep_means_a_module_is_alive_again(self):
        clock = Clock()
        policy = Policy(PolicyConfig(asleep_confirm_cycles=1), clock=clock)
        policy.decide(SILENT)
        clock.advance(300.0)
        decision = policy.decide(REFUSING)
        self.assertEqual(decision.state, NOT_SERVING)
        self.assertEqual(policy.quiet_since, clock.now)

    def test_the_wake_threshold_only_applies_from_asleep(self):
        # From RECENTLY_PARKED the OBD evidence is still arriving and is much
        # better than a voltage, so the voltage is not given a vote.
        clock = Clock()
        policy = parked_policy(clock)
        decision = policy.decide(Observation(no_data=True, can_status_clean=True,
                                             volts=DCDC_RUNNING))
        self.assertEqual(decision.state, RECENTLY_PARKED)


class TestFaultIsNotSleep(unittest.TestCase):
    """NO DATA with non-zero CAN counters is a fault, and must stay visible."""

    def test_a_fault_changes_absolutely_nothing(self):
        clock = Clock()
        policy = parked_policy(clock)
        before = (policy.state, policy.asleep_streak, policy.quiet_since)
        clock.advance(45.0)
        decision = policy.decide(FAULT)
        self.assertEqual((policy.state, policy.asleep_streak, policy.quiet_since),
                         before)
        self.assertEqual(decision.state, RECENTLY_PARKED)
        self.assertEqual(decision.interval_s, 45.0)
        self.assertEqual(token(decision), "can_fault_hold")
        self.assertIn("fault", decision.reason)

    def test_a_fault_never_confirms_sleep_however_long_it_lasts(self):
        clock = Clock()
        policy = parked_policy(clock)
        for _ in range(50):
            clock.advance(60.0)  # well past parked_window_s
            decision = policy.decide(FAULT)
            self.assertEqual(decision.state, RECENTLY_PARKED)
        self.assertEqual(policy.asleep_streak, 1)

    def test_a_fault_does_not_advance_the_streak_between_silent_cycles(self):
        clock = Clock()
        policy = Policy(clock=clock)
        policy.decide(SILENT)
        policy.decide(FAULT)
        policy.decide(FAULT)
        self.assertEqual(policy.asleep_streak, 1)
        self.assertEqual(policy.decide(SILENT).state, RECENTLY_PARKED)
        self.assertEqual(policy.decide(SILENT).state, ASLEEP)

    def test_a_fault_while_driving_holds_the_driving_interval(self):
        policy = Policy(clock=Clock())
        policy.decide(Observation(had_data=True, moving=True))
        decision = policy.decide(FAULT)
        self.assertEqual(decision.state, DRIVING)
        self.assertEqual(decision.interval_s, 2.0)

    def test_an_unparsed_can_status_changes_absolutely_nothing(self):
        # An ATCS reply that could not be read is not evidence of a silent bus
        # or of a broken one.
        clock = Clock()
        policy = parked_policy(clock)
        before = (policy.state, policy.asleep_streak, policy.quiet_since)
        clock.advance(1000.0)
        decision = policy.decide(UNPARSED)
        self.assertEqual((policy.state, policy.asleep_streak, policy.quiet_since),
                         before)
        self.assertEqual(decision.state, RECENTLY_PARKED)
        self.assertEqual(token(decision), "can_status_unknown_hold")

    def test_an_unparsed_can_status_never_confirms_sleep(self):
        clock = Clock()
        policy = parked_policy(clock)
        for _ in range(50):
            clock.advance(60.0)
            self.assertEqual(policy.decide(UNPARSED).state, RECENTLY_PARKED)

    def test_a_refusal_outranks_the_fault_hold(self):
        # A module answering proves something is on the bus, whatever the
        # counters say about the requests that got no reply.
        clock = Clock()
        policy = Policy(clock=clock)
        decision = policy.decide(Observation(conditions_not_correct=True,
                                             no_data=True, can_status_clean=False))
        self.assertEqual(decision.state, NOT_SERVING)


class TestBatteryGuard(unittest.TestCase):
    def test_a_low_pisugar_cell_stops_immediately(self):
        # battery.py has already required its own run of low readings, so
        # re-counting them here would only delay a decision made carefully.
        policy = Policy(clock=Clock())
        decision = policy.decide(Observation(had_data=True, battery_low=True))
        self.assertEqual(decision.state, LOW_BATTERY)
        self.assertTrue(decision.stop)
        self.assertFalse(decision.obd_allowed)

    def test_one_low_rail_reading_does_nothing(self):
        policy = Policy(clock=Clock())
        decision = policy.decide(Observation(had_data=True, volts=12.0))
        self.assertEqual(decision.state, AWAKE)
        self.assertFalse(decision.stop)
        self.assertEqual(policy.low_volts_streak, [12.0])

    def test_the_configured_run_of_low_rail_readings_stops(self):
        policy = Policy(clock=Clock())
        states = [policy.decide(Observation(had_data=True, volts=12.0)).state
                  for _ in range(3)]
        self.assertEqual(states, [AWAKE, AWAKE, LOW_BATTERY])

    def test_a_reading_at_the_threshold_counts(self):
        policy = Policy(PolicyConfig(low_volts_consecutive=1), clock=Clock())
        self.assertEqual(policy.decide(Observation(volts=12.2)).state, LOW_BATTERY)

    def test_a_healthy_reading_breaks_the_run(self):
        policy = Policy(clock=Clock())
        policy.decide(Observation(had_data=True, volts=12.0))
        policy.decide(Observation(had_data=True, volts=12.0))
        policy.decide(Observation(had_data=True, volts=ASLEEP_RESTING))
        self.assertEqual(policy.low_volts_streak, [])
        self.assertEqual(policy.decide(Observation(had_data=True, volts=12.0)).state,
                         AWAKE)

    def test_a_missing_reading_breaks_the_run(self):
        # A flapping ATRV must not accumulate towards a stop.
        policy = Policy(clock=Clock())
        policy.decide(Observation(had_data=True, volts=12.0))
        policy.decide(Observation(had_data=True, volts=None))
        self.assertEqual(policy.low_volts_streak, [])

    def test_the_resting_voltage_is_above_the_stop_threshold(self):
        # 12.7 V asleep is normal.  Stopping on it would end every sleep
        # observation this project wants to run.
        policy = Policy(clock=Clock())
        for _ in range(10):
            decision = policy.decide(Observation(volts=ASLEEP_RESTING))
            self.assertFalse(decision.stop)

    def test_a_bus_fault_does_not_suppress_the_battery_stop(self):
        # ATRV reads a pin inside the adapter and never touches the bus, so a
        # CAN fault does not make the voltage less true -- and stopping is the
        # conservative act either way.
        policy = Policy(clock=Clock())
        low_fault = Observation(no_data=True, can_status_clean=False, volts=12.0)
        states = [policy.decide(low_fault).state for _ in range(3)]
        self.assertEqual(states[-1], LOW_BATTERY)
        self.assertTrue(policy.decide(low_fault).stop)

    def test_a_recovered_battery_resumes_the_state_it_left(self):
        clock = Clock()
        policy = parked_policy(clock)
        self.assertEqual(policy.decide(Observation(battery_low=True)).state,
                         LOW_BATTERY)
        decision = policy.decide(Observation(volts=ASLEEP_RESTING))
        self.assertEqual(decision.state, RECENTLY_PARKED)
        self.assertFalse(decision.stop)
        self.assertTrue(decision.reason.startswith("battery_recovered;"))

    def test_a_recovered_battery_still_reads_the_same_observation(self):
        policy = Policy(clock=Clock())
        policy.decide(Observation(battery_low=True))
        decision = policy.decide(Observation(had_data=True, moving=True,
                                             volts=DCDC_RUNNING))
        self.assertEqual(decision.state, DRIVING)

    def test_a_policy_started_in_low_battery_can_recover(self):
        policy = Policy(state=LOW_BATTERY, clock=Clock())
        self.assertEqual(policy.decide(Observation(volts=DCDC_RUNNING)).state, AWAKE)


class TestIntervalFloor(unittest.TestCase):
    def test_the_floor_applies_in_every_state(self):
        floor = 600.0
        cfg = PolicyConfig(floor_interval_s=floor)
        for state in STATES:
            self.assertEqual(cfg.interval_for(state), floor, state)

    def test_the_floor_can_only_make_the_node_gentler(self):
        # An override that could lower an interval would let an operator poll
        # a sleeping vehicle harder than the state machine ever would.
        cfg = PolicyConfig(floor_interval_s=0.1)
        self.assertEqual(cfg.interval_for(DRIVING), 2.0)
        self.assertEqual(cfg.interval_for(ASLEEP), 300.0)

    def test_a_floored_policy_reports_the_floored_interval(self):
        clock = Clock()
        floor = 600.0
        policy = Policy(PolicyConfig(floor_interval_s=floor), clock=clock)
        seen = []
        for observation in (Observation(had_data=True, moving=True), SERVING,
                            REFUSING, SILENT, Observation(battery_low=True)):
            decision = policy.decide(observation)
            seen.append(decision.state)
            self.assertEqual(decision.interval_s, floor, decision.state)
        self.assertEqual(seen, [DRIVING, AWAKE, NOT_SERVING, RECENTLY_PARKED,
                                LOW_BATTERY])

    def test_a_floor_below_a_state_interval_leaves_it_alone(self):
        clock = Clock()
        policy = Policy(PolicyConfig(floor_interval_s=30.0), clock=clock)
        self.assertEqual(policy.decide(Observation(had_data=True, moving=True)
                                       ).interval_s, 30.0)
        self.assertEqual(policy.decide(REFUSING).interval_s, 45.0)

    def test_the_floor_survives_a_hold(self):
        clock = Clock()
        policy = Policy(PolicyConfig(floor_interval_s=90.0), clock=clock)
        policy.decide(SERVING)
        self.assertEqual(policy.decide(FAULT).interval_s, 90.0)


class TestHolds(unittest.TestCase):
    def test_an_empty_observation_holds_the_current_state(self):
        # Every Observation field defaults to "no evidence", so a caller that
        # could not fill one in holds rather than advances.
        policy = Policy(clock=Clock())
        policy.decide(Observation(had_data=True, moving=True))
        decision = policy.decide(Observation())
        self.assertEqual(decision.state, DRIVING)
        self.assertEqual(token(decision), "no_signal_hold")

    def test_a_hold_names_the_state_it_is_holding(self):
        policy = Policy(clock=Clock())
        policy.decide(SERVING)
        self.assertIn(AWAKE, policy.decide(Observation()).reason)


class TestPurity(unittest.TestCase):
    def test_the_module_imports_no_transport_or_session(self):
        # The point of the separation: every transition above was reachable
        # without a fake adapter, and it stays that way only if nothing here
        # ever needs one.
        import hummer_obd.policy as policy_module

        with open(policy_module.__file__) as fh:
            source = fh.read()
        for forbidden in ("from .transport", "from .session",
                          "import transport", "import session"):
            self.assertNotIn(forbidden, source)

    def test_the_default_clock_is_used_when_no_time_is_given(self):
        policy = Policy()
        self.assertEqual(policy.decide(SERVING).state, AWAKE)

    def test_decisions_are_immutable(self):
        decision = Policy(clock=Clock()).decide(SERVING)
        with self.assertRaises(Exception):
            decision.interval_s = 0.1


if __name__ == "__main__":
    unittest.main()
