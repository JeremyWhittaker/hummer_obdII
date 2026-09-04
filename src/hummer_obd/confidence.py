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
        "a temperature the vehicle holds. 0x2AF1's array lands within 1.5-2.0 C "
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
    "27BB": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "thermal-management energy candidate; answers, stored raw"),
    "27B5": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "thermal-management distance candidate; answers, stored raw"),
    "2709": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "A/C compressor temperature candidate. Moved monotonically through the 2026-09-04 charge while "
                     "the pack warmed 16.2 F, which is consistent with a thermal "
                     "quantity. No scaling follows: a least-squares fit against "
                     "temp_f lands on 1/1.3 C per count and no designer picks "
                     "that. Over a monotonic ramp any two rising quantities fit "
                     "a line, so a believable slope means a round divisor that "
                     "holds across a SECOND charge warming at a different "
                     "rate. The same charge demonstrated the hazard "
                     "directly: over its first four minutes charge power "
                     "correlated with temp_f at +0.72, and over the full "
                     "twenty minutes including the recovery at -0.028, "
                     "with the hardest and slowest charge rates occurring "
                     "at the SAME 107.6 F"),
    "4149": Evidence(
        1, ("40",), ("parked", "driving"),
        "EVSE advertised current candidate. Read 384 while parked and "
        "unplugged, which is the state that says least about an EVSE current. "
        "A charge session is what decides it"),
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
                     "throughout. A field that holds still while the thing it "
                     "allegedly measures changes is not measuring it, which is "
                     "this project's own rule turned on its own candidate. "
                     "Evidence against the source's label, not proof: a slow "
                     "update cadence or a different thermal zone would look the "
                     "same over ten minutes. Reads 0x46 "
                     "throughout"),
    "4127": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "battery temperature A candidate. Across 52 charging samples on 2026-09-04 the pack warmed "
                     "16.2 F and this field DID NOT MOVE -- one distinct value "
                     "throughout. A field that holds still while the thing it "
                     "allegedly measures changes is not measuring it, which is "
                     "this project's own rule turned on its own candidate. "
                     "Evidence against the source's label, not proof: a slow "
                     "update cadence or a different thermal zone would look the "
                     "same over ten minutes. Reads 0x0418 "
                     "throughout"),
    "4124": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "battery temperature B candidate. Across 52 charging samples on 2026-09-04 the pack warmed "
                     "16.2 F and this field DID NOT MOVE -- one distinct value "
                     "throughout. A field that holds still while the thing it "
                     "allegedly measures changes is not measuring it, which is "
                     "this project's own rule turned on its own candidate. "
                     "Evidence against the source's label, not proof: a slow "
                     "update cadence or a different thermal zone would look the "
                     "same over ten minutes. It reads 0x0000 "
                     "throughout, which is not a temperature in any scaling"),
    "40E5": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "battery coolant temperature 1 candidate. Moved monotonically through the 2026-09-04 charge while "
                     "the pack warmed 16.2 F, which is consistent with a thermal "
                     "quantity. No scaling follows: a least-squares fit against "
                     "temp_f lands on 1/17.2 C per count and no designer picks "
                     "that. Over a monotonic ramp any two rising quantities fit "
                     "a line, so a believable slope means a round divisor that "
                     "holds across a SECOND charge warming at a different "
                     "rate. The same charge demonstrated the hazard "
                     "directly: over its first four minutes charge power "
                     "correlated with temp_f at +0.72, and over the full "
                     "twenty minutes including the recovery at -0.028, "
                     "with the hardest and slowest charge rates occurring "
                     "at the SAME 107.6 F"),
    "40E6": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "battery coolant temperature 2 candidate. Its charging "
                     "values are DISJOINT from every one of 566 parked samples. "
                     "Moved monotonically through the 2026-09-04 charge while "
                     "the pack warmed 16.2 F, which is consistent with a thermal "
                     "quantity. No scaling follows: a least-squares fit against "
                     "temp_f lands on 1/5.7 C per count and no designer picks "
                     "that. Over a monotonic ramp any two rising quantities fit "
                     "a line, so a believable slope means a round divisor that "
                     "holds across a SECOND charge warming at a different "
                     "rate. The same charge demonstrated the hazard "
                     "directly: over its first four minutes charge power "
                     "correlated with temp_f at +0.72, and over the full "
                     "twenty minutes including the recovery at -0.028, "
                     "with the hardest and slowest charge rates occurring "
                     "at the SAME 107.6 F"),

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
