"""How much each allowlisted identifier has actually been proven.

The safety gate answers one question: *may this be transmitted?* It is a
`dict[str, str]` of identifier to provenance prose, and that shape is right for
what it does. It cannot answer the question a reader of the telemetry actually
has, which is *how much should I believe this number?*

Today that answer lives in prose, in three places, maintained by hand:
`docs/TELEMETRY_CATALOG.md` grades each signal `measured` / `read` / `raw`,
`docs/GM_ENHANCED_CANDIDATES.md` sorts identifiers into tiers, and the gate's own
strings end `UNMERGED` or `not BT1`. Three hand-kept inventories over one set of
facts is how the identifier registry drifted thirty-six commits behind the code,
and `registry.py` was written to stop exactly that. This is the same fix applied
one layer up.

**The dict type is deliberately not changed.** `ENHANCED_READ_DIDS` stays
`dict[str, str]`, so `enhanced.py`, `registry.py` and their tests are untouched;
this is a parallel table, and a test asserts the two key sets are identical. An
identifier cannot exist in one and not the other.

The levels
----------

===== ================================================================
Level Meaning
===== ================================================================
0     Sourced, allowlisted, and never confirmed to answer here.
1     A module on this vehicle returned a positive response. No
      meaning is claimed: the bytes are stored raw.
2     Decodes to a plausible value with a stated scaling. Nothing
      independent confirms that scaling.
3     Cross-validated: a second, independent route to the same
      quantity agrees.
4     Level 3, and the cross-validation itself has been re-derived in
      more than one vehicle state.
===== ================================================================

**Only level 3 and above is a production telemetry field.** Everything below is
either raw evidence being accumulated or a candidate awaiting a state that will
decide it. The distinction matters because level 2 is the dangerous one: a
plausible number with a confident-looking unit beside it, and nothing behind it.
This project has already published a scaling from a source that this vehicle
then contradicted -- `0x5401`, "charger DC power", which answers with a single
byte and reads non-zero while idle. That identifier is level 1 and stays there.

The gap between 3 and 4 is not pedantry either. A relationship measured only
while parked, or only while charging, can be an artefact of that state. The
`0x2885` x `0x2414` product agrees with the energy field's slope both during an
AC charge and during a 97.8 kW pull; that is what makes it a level 4 rather than
a coincidence that held once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .safety import ENHANCED_READ_DIDS

__all__ = [
    "Evidence",
    "CONFIDENCE",
    "LEVEL_NAMES",
    "PRODUCTION_MINIMUM",
    "at_least",
    "unproven",
]

#: A field below this is not a telemetry reading, whatever its column is called.
PRODUCTION_MINIMUM: Final[int] = 3

LEVEL_NAMES: Final[dict[int, str]] = {
    0: "sourced only",
    1: "answers here",
    2: "decoded",
    3: "cross-validated",
    4: "cross-validated in more than one state",
}


@dataclass(frozen=True)
class Evidence:
    """What is known about one identifier, and how it came to be known."""

    level: int
    #: Module addresses on this vehicle that returned a positive response.
    #: Empty at level 0 -- which is the definition of level 0.
    answers_at: tuple[str, ...]
    #: Vehicle states the identifier has been observed in.  A field seen only
    #: while parked has been seen in the state that says least.
    states: tuple[str, ...]
    #: Why it is at this level.  At level 3 and above this must name the
    #: independent route that agrees; a test checks that it does.
    basis: str


_CB = ("CB",)
_PACK = ("17", "1D", "1E")

#: The shared preamble for every field graded by the 2026-09-04 HVAC A-B-A run.
#: It is a constant rather than repeated prose so the phase boundaries can never
#: drift between entries -- six fields were graded off this one experiment, and
#: six hand-copied timestamps is six chances to mistype one.
#:
#: The A-B-A shape is the whole point.  Three earlier attempts to read HVAC
#: fields used a single transition, and a single transition cannot tell a mode
#: response from a field that was simply going to rise anyway: 0x27BB rose
#: during A/C, rose again during heat, and would have been published as an A/C
#: response had the second A/C phase not shown it climbing straight through the
#: reversal.  Returning to the first condition is what makes the difference
#: observable.
ABA = (
    "The 2026-09-04 A-B-A experiment (cold soak 15:19-15:34, A/C max "
    "15:34-15:49, heat max 15:49-15:58, A/C max 15:58-16:07:53; owner "
    "operated the controls and reported each switch, phases marked via "
    "hummer-obd-experiment) is what settles this. Three single transitions "
    "cannot separate mode from elapsed time; only the return to A/C can. "
    "The second A/C phase ends at first wheel motion, 16:07:53Z, when the "
    "owner drove to work -- an unbounded window would quietly become a "
    "driving window and stop being an A/C phase at all. Figures below are "
    "per-phase over 99/80/44/48 samples and EXCLUDE the switch minutes "
    "themselves, so a transient at a transition falls in a gap, not a phase. "
)

#: Keyed identically to :data:`hummer_obd.safety.ENHANCED_READ_DIDS`.  A test
#: asserts the key sets match, so adding to the gate without recording what is
#: known about the addition fails the suite.
CONFIDENCE: Final[dict[str, Evidence]] = {
    # -- level 4: cross-validated, and re-derived in more than one state ----
    "27C6": Evidence(
        4, _CB, ("parked", "driving", "charging"),
        "state of charge. Cross-validated against 0x27AF: the energy/SoC ratio "
        "holds a constant 1.919 kWh per percent across drives and a charge, "
        "which is two independently scaled fields agreeing on a pack size. "
        "Separately, 0x2B43 tracks it at r=+0.995 over the drive corpus"),
    "27AF": Evidence(
        4, _CB, ("parked", "driving", "charging"),
        "energy remaining. Its 60-second slope is the project's charge/discharge "
        "power route, and that slope agreed with 0x2885 x 0x2414 within 6% "
        "during an AC charge and again during a 97.8 kW pull -- a different "
        "identifier, a different module, a different method"),
    "2AF5": Evidence(
        4, _CB, ("parked", "driving", "charging"),
        "cell voltage average/minimum/maximum. Cross-validated against 0x2885: "
        "cell average x 96 lands on the measured pack voltage, in every state "
        "sampled. The trailing four bytes are level 1 and stored raw"),
    "2885": Evidence(
        4, _PACK, ("parked", "driving", "charging"),
        "traction pack voltage. Three independent routes agree: 96 x cell "
        "average from 0x2AF5, the voltage-ratio pack structure, and the product "
        "with 0x2414 against the 0x27AF slope. Answers identically at all three "
        "drive motor controllers"),
    "2414": Evidence(
        4, ("17",), ("parked", "driving", "charging"),
        "traction pack current. Cross-validated by its product with 0x2885 "
        "against the 0x27AF energy slope, both while charging (8.14 kW vs "
        "~7.7 kW) and while discharging. The source's sign convention -- "
        "negative is charging -- was confirmed on this vehicle while plugged in"),

    # -- level 3: cross-validated once, or in one state --------------------
    "27C7": Evidence(
        3, _CB, ("parked", "driving", "charging"),
        "estimated range. Corroborated against the vehicle's EPA figure: the "
        "reading extrapolates to 333 mi at full charge against a 329 mi rating. "
        "That is one comparison against an external number, not a second "
        "on-vehicle route, which is why it is 3 and not 4"),
    "33E5": Evidence(
        3, _PACK, ("parked", "driving"),
        "module supply voltage, the 12 V domain and not the pack. Modules 17, "
        "1D and 1E answer independently and agree with each other (13.2/13.1/"
        "13.1 V), and a third route -- legislated PID 0142 -- was added on "
        "2026-09-04, giving three readings of one rail. They differ, and the "
        "differences are multiplicative: over 358 paired samples the ratios "
        "hold to 0.53% while the offsets wander by 23%, and the gap does not "
        "widen across 387 kW of traction power. That is a scaling difference "
        "between uncalibrated ADCs rather than a decode error, but which of the "
        "three is right needs a reference meter this project does not have -- "
        "which is exactly why this is not 4"),

    # -- level 2: decoded, nothing independent confirms the scaling --------
    "27C0": Evidence(
        2, _CB, ("parked", "driving", "charging"),
        "distance since full charge. Plausible and monotonic within a session; "
        "nothing independent confirms the divisor. The odometer would settle it "
        "and that comparison has not been run"),
    "0046": Evidence(
        2, _CB, ("parked", "driving", "charging"),
        "a PACK-SIDE temperature, settled 2026-09-04. It reads 95.0 F while "
        "the truck's own display shows 94 F -- but it rose 93.2 to 111.2 F "
        "during an overnight charge, and garage ambient cannot move 18 F at "
        "2 a.m., so it is not an ambient sensor. The two agree this morning "
        "because the pack has equilibrated to the garage, which is a "
        "convergence rather than an identity. 0x2AF1's array lands within 1.5-2.0 C "
        "of it under one candidate scaling, which is one sample at one "
        "temperature and therefore not a confirmation. The corpus spans 23.4 F "
        "as of 2026-09-04 -- it said 5.4 F when this was written, and the "
        "figure had quadrupled without anything noticing -- but 0x2AF1's own "
        "rows still cover only 9.0 F of that, and across those the strongest "
        "correlation any byte window reaches against temp_f is +0.69"),
    "4A7A": Evidence(
        3, ("28",), ("parked", "driving"),
        "wheel speed, four corners. Cross-validated against legislated PID "
        "010D, recorded in the same row from a different module: r=+0.997 on "
        "each corner over 670 moving samples spanning 1-130 km/h, with a mean "
        "difference within 0.1 km/h of zero. A vendor scaling from an unmerged "
        "BEV3 source confirmed by the standard's own measurement. "
        "tests/test_confidence.py re-derives this from the committed sessions"),
    "4A7C": Evidence(
        2, ("28",), ("parked", "driving"),
        "brake pressure. Scaling derived from the source's own test vectors and "
        "verified against every one of them; nothing on this vehicle confirms it"),
    "4C2D": Evidence(
        2, ("28",), ("parked", "driving"),
        "steering wheel angle. Scaling derived from the source's test vectors; "
        "no independent measurement of steering exists on this vehicle"),
    "4C2F": Evidence(
        2, ("28",), ("parked", "driving"),
        "lateral acceleration. Scaling from the source's test vectors. Reads "
        "near zero parked, which is consistent and is not a confirmation. Its "
        "sibling 0x4C30 shares the scaling and is confirmed at level 3, which "
        "is suggestive and is not evidence about this one: nothing here "
        "measures cornering independently"),
    "4C30": Evidence(
        3, ("28",), ("parked", "driving"),
        "longitudinal acceleration. Cross-validated against the derivative of "
        "legislated PID 010D: r=+0.837 over 1683 samples, and -- the stronger "
        "part -- the magnitudes match, -2.71..+2.60 m/s2 from the speed "
        "derivative against -3.00..+3.19 m/s2 from the field. The correlation "
        "is not higher because the two are read seconds apart and a derivative "
        "over an 8-second cycle is a smoothed version of an accelerometer; "
        "that is a sampling limit, not a disagreement"),

    # -- level 1: answers here, meaning not claimed ------------------------
    "2B43": Evidence(
        1, _CB, ("parked", "driving", "charging"),
        "26-value array. Tracks state of charge at r=+0.995 across the drive "
        "corpus, which is a direction and not a scaling; stored raw"),
    "2AF1": Evidence(
        1, _CB, ("parked", "driving", "charging"),
        "24-value array. Twenty-four is the module count three independent "
        "structural results agree on, which is suggestive and is not a decode; "
        "stored raw"),
    "5401": Evidence(
        2, _CB, ("parked", "driving", "charging"),
        "the identifier this vehicle contradicted its source about, now "
        "positively identified as a state rather than a quantity. Published as "
        "two-byte charger DC power / 4350; it answers with a SINGLE byte and "
        "plateaus across a 9x power range, so the published scaling is wrong "
        "here. What it does do is switch cleanly: 0x00 across 566 consecutive "
        "parked-and-unplugged samples on 2026-09-04, and 0x93/0x96 across every "
        "one of the 22 samples taken while charging -- completely disjoint sets, "
        "cross-checked against pack current being negative. So it is a "
        "charging-state indicator. No scaling is claimed and none is applied; "
        "why it alternates between 0x93 and 0x96 while charging is not known, "
        "which is most of why this is 2 and not 3"),
    "27BF": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "regeneration-related candidate; answers, stored raw"),
    "27BB": Evidence(2, _CB, ("parked", "driving", "charging"),
                     "thermal-management energy candidate, and the A-B-A "
                     "supports the energy half of that name. " + ABA + "This "
                     "field runs 0 through the cold soak, then 10 to 60 "
                     "across the first A/C phase, 70 to 110 across heat, and "
                     "120 to 150 across the second A/C phase: monotonically "
                     "non-decreasing across a mode REVERSAL, in steps of 10, "
                     "starting from zero. That is what an accumulator does "
                     "and what no temperature or mode state can do. The "
                     "practical consequence matters more than the label: "
                     "this field must never be read as responding to HVAC "
                     "mode. It rose during A/C and rose during heat because "
                     "it integrates, and reading its A/C rise as an A/C "
                     "response is exactly the elapsed-time error this "
                     "experiment was built to catch. No scaling is claimed; "
                     "the units of the step are unknown"),
    "27B5": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "thermal-management distance candidate; answers, stored raw"),
    "2709": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "A/C compressor temperature candidate. " + ABA + "Before "
                     "looking at the result this project recorded the "
                     "prediction that, if this is genuinely A/C compressor "
                     "temperature, it should rise under A/C and not under "
                     "heat. It does not discriminate: 101-104 cold, 106-110 "
                     "under A/C, 107-112 under heat, 110-112 under A/C again. "
                     "The heat and A/C bands overlap almost entirely, so this "
                     "field cannot say which mode is running, and that is the "
                     "narrow thing the experiment establishes. It rises "
                     "across the run and then turns over slightly in the last "
                     "phase (112 down to 110), so it is not a clean "
                     "accumulator either. The prediction is what failed, and "
                     "it is not clear the label did: GM's Ultium vehicles are "
                     "marketed with heat-pump and waste-heat-recovery thermal "
                     "systems, and if the compressor runs in heat mode too "
                     "then warming in both modes is exactly what a compressor "
                     "temperature should do. That is an unresolved "
                     "alternative, not a conclusion -- it has not been sourced "
                     "for this VIN. Still no scaling: a least-squares fit "
                     "against temp_f lands on 1/1.3 C per count and no "
                     "designer picks that"),
    "4149": Evidence(
        1, ("40",), ("parked", "driving"),
        "EVSE advertised current candidate, and a lesson in checking WHEN a "
        "value appeared. It held 0x00A0 = 160 across all 147 samples of the "
        "2026-09-04 charge while a JuiceBox read 40.2 A twice, and 160/4 = "
        "40.0 -- which was written up as strong circumstantial support. It is "
        "not. The value had already been 160 for **124 minutes before the "
        "charger was connected**, so the charge did not produce it and the "
        "match is coincidence. Across the corpus the field takes 36, 96, 100, "
        "160, 384, 385, 388 and 389, changing repeatedly while nothing is "
        "plugged in at all, and it read 384 the morning after unplugging. Two "
        "loose clusters, no relation to a connected EVSE that this data "
        "supports. Stored raw, level 1, and the divisor is not claimed"),
    "416C": Evidence(
        1, ("40",), ("parked", "driving"),
        "battery group voltage 1 candidate. Read 2589 then 2593 a minute apart, "
        "so it moves; 0x416D and 0x416E returned identical values to each other, "
        "which a genuine per-group voltage would be unlikely to do"),
    "416D": Evidence(1, ("40",), ("parked", "driving"),
                     "battery group voltage 2 candidate; identical to 0x416E "
                     "when read, stored raw"),
    "416E": Evidence(1, ("40",), ("parked", "driving"),
                     "battery group voltage 3 candidate; identical to 0x416D "
                     "when read, stored raw"),
    "434F": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "HV battery temperature candidate. Across 52 charging samples on 2026-09-04 the pack warmed "
                     "16.2 F and this field DID NOT MOVE -- one distinct value "
                     "throughout. That was written up as evidence against the "
                     "source's label. The morning after weakens it: the field "
                     "DOES take other values in other states, so it is not "
                     "dead, it simply held still through a charge. The rule -- "
                     "a field that does not move while the thing it allegedly "
                     "measures does is not measuring it -- still applies to "
                     "that window, and says less than it first appeared to. Reads 0x46 "
                     "throughout"),
    "4127": Evidence(2, ("40",), ("parked", "driving", "charging"),
                     "candidate labelled battery temperature A by the source. "
                     "It is NOT a temperature. " + ABA + "It holds ONE value "
                     "per phase: 234 across all 99 cold-soak samples and all "
                     "80 of the first A/C phase, then exactly 601 across all "
                     "44 heat samples, then 234 again. Not a range per phase "
                     "-- a single constant, stepping on the edges the owner "
                     "commanded, at 15:50:02 and back at 15:59:23, each "
                     "within one poll of the switch. No pack temperature is "
                     "constant to the count for 179 samples and then moves "
                     "367 in one poll and back inside nine minutes. "
                     "Corpus-wide it takes 234, 238, 242, 246, 261, 429, 601 "
                     "and 1048, and 1048 occurs in 410 samples of which every "
                     "one has negative pack current -- it appears only while "
                     "charging. So it is a thermal-mode or heat-request state "
                     "word. This supersedes the earlier "
                     "held-still-through-a-charge argument, which was true "
                     "but far weaker: a field holding still is ambiguous, a "
                     "field that moves on a commanded edge and returns is "
                     "not. No scaling is claimed and none is applied. It is 2 "
                     "and not 3 because this is ONE heat cycle; a second one "
                     "on a different day, reproducing 601, is what would "
                     "make it 3"),
    "4124": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "candidate labelled battery temperature B by the source. "
                     "It is NOT a temperature. " + ABA + "It reads exactly "
                     "1000 in every one of the 271 samples across all four "
                     "phases -- one distinct value per phase, and the same "
                     "value in all of them -- so it carries no thermal "
                     "magnitude and does not distinguish A/C from heat. It "
                     "leaves 1000 only in brief transients AT the switches, "
                     "which is why they fall in the gaps between phases and "
                     "not inside any of them: 418 at 15:34:20 back to 1000 "
                     "by 15:34:28 (A/C on, an eight-second excursion), and "
                     "910 at 15:50:12 back to 1000 by 15:50:51 (heat on). It "
                     "also read 0x0000 for the whole 2026-09-04 charge, which "
                     "is not a temperature in any scaling. Kept at 1 "
                     "deliberately: the negative finding is solid, but two "
                     "transients are far too thin to publish a positive "
                     "reading of what the dip means"),
    "40E5": Evidence(2, ("40",), ("parked", "driving", "charging"),
                     "battery coolant temperature 1 candidate, and the only "
                     "continuous field here that responds to HVAC mode "
                     "reversibly. " + ABA + "It holds flat at 860 through "
                     "the cold soak, ramps 890 to 980 across the first A/C "
                     "phase, jumps to 1125-1170 under heat, and comes back "
                     "to 980-985 under A/C again -- returning to where the "
                     "first A/C phase ended rather than continuing to climb. "
                     "A field that goes up with heat, comes back down when "
                     "heat stops, and lands on its own earlier value is "
                     "tracking the thermal system's state, not the clock. "
                     "That is a real mode response and it is the strongest "
                     "result of the experiment for a non-state field. Still "
                     "no scaling is claimed: a least-squares fit against "
                     "temp_f lands on 1/17.2 C per count and no designer "
                     "picks that, and this experiment constrains the field's "
                     "BEHAVIOUR without constraining its units at all. 2 not "
                     "3 for the same reason as 0x4127 -- one heat cycle"),
    "40E6": Evidence(2, ("40",), ("parked", "driving", "charging"),
                     "battery coolant temperature 2 candidate. It responds to "
                     "HVAC being on, but carries no mode information. " + ABA +
                     "Across the 99 cold-soak samples it runs 696-808 and is "
                     "still drifting; from A/C on it drops into 437-505 and "
                     "every later phase stays in that neighbourhood -- 485-516 "
                     "under heat, 353-524 under A/C again. The separation at "
                     "HVAC-on is real: the coldest cold-soak sample, 696, is "
                     "well above the warmest A/C sample, 505. The "
                     "heat-versus-A/C difference is not -- those two bands "
                     "overlap, so this field cannot tell the modes apart. An "
                     "earlier draft of this entry quoted 806-808 for the cold "
                     "soak and 484-524 for the second A/C phase; both came "
                     "from truncated windows (the last four minutes of the "
                     "soak, and 23 of the 48 available A/C samples) and are "
                     "corrected here. Its charging values are also DISJOINT "
                     "from every one of 566 parked samples, which is a second "
                     "independent state separation. No scaling is claimed: a "
                     "least-squares fit against temp_f lands on 1/5.7 C per "
                     "count and no designer picks that"),

    # -- level 0: allowlisted, never answered here -------------------------
    #
    # The four ISO identifiers are a deliberate and permanent level 0, and the
    # only entries here where that is not a gap.  They are sent to find out
    # whether a module is reachable, and a formed `7F 22 31` answers that
    # perfectly well -- better, in fact, than a positive response would, because
    # it also proves the module parses service 22 rather than echoing.
    "F187": Evidence(
        0, (), ("probe only",),
        "ISO 14229-1 spare part number. Never returned a positive response from "
        "any module; 40, CD and 45 all answer 7F 22 31. That is what it is for: "
        "a formed refusal proves reachability"),
    "F188": Evidence(0, (), ("probe only",),
                     "ISO 14229-1 ECU software number; reachability probe, "
                     "7F 22 31 everywhere asked"),
    "F189": Evidence(0, (), ("probe only",),
                     "ISO 14229-1 ECU software version; reachability probe, "
                     "7F 22 31 everywhere asked"),
    "F191": Evidence(0, (), ("probe only",),
                     "ISO 14229-1 ECU hardware number; reachability probe, "
                     "7F 22 31 everywhere asked"),
    "2429": Evidence(
        1, ("17",), ("parked", "driving"),
        "the source calls this nominal (rated) pack voltage, /64. This project "
        "believed that for about three hours on 2026-09-04 and it is wrong. "
        "The first reading, 22534, divided by 64 gives 352.09 V -- which across "
        "96 series cells is 3.6676 V, the textbook NMC nominal to four figures, "
        "from a number nobody fitted. Extremely convincing, and one sample. "
        "Across 405 samples it spans 18556-26588 raw with 108 distinct values, "
        "and it moves WITH LOAD: r=+0.83 against pack current and HV power, "
        "r=-0.67 against pack voltage, and nothing against state of charge "
        "(-0.09) or energy (-0.08). It rests near 22350 and rises about 16.4 "
        "counts per amp of discharge. A rated figure does not move and a "
        "voltage does not rise with current. Stored raw, meaning unclaimed -- "
        "the second identifier whose published label this vehicle contradicted, "
        "after 0x5401"),
}


def at_least(level: int) -> tuple[str, ...]:
    """Identifiers at or above *level*, sorted."""
    return tuple(sorted(d for d, e in CONFIDENCE.items() if e.level >= level))


def unproven() -> tuple[str, ...]:
    """Identifiers this vehicle has never returned a positive response for."""
    return tuple(sorted(d for d, e in CONFIDENCE.items() if e.level == 0))


# The parity that makes this table trustworthy, asserted at import so a gate
# edit that forgets this file fails before anything can read a level from it.
assert set(CONFIDENCE) == set(ENHANCED_READ_DIDS), (
    "the confidence table and the safety gate must hold the same identifiers; "
    f"only in gate: {sorted(set(ENHANCED_READ_DIDS) - set(CONFIDENCE))}; "
    f"only in table: {sorted(set(CONFIDENCE) - set(ENHANCED_READ_DIDS))}"
)
assert all(e.level in LEVEL_NAMES for e in CONFIDENCE.values())
assert all(bool(e.answers_at) == (e.level >= 1) for e in CONFIDENCE.values()), (
    "level 0 means no module here answered it, and level 1+ means one did; "
    "answers_at has to agree with the level"
)
