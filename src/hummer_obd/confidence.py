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
        3, _PACK, ("parked", "driving", "charging"),
        "module supply voltage, the 12 V domain and not the pack. Modules 17, "
        "1D and 1E answer independently and agree with each other, and a third "
        "route -- legislated PID 0142 -- was added on 2026-09-04, giving three "
        "readings of one rail. AN EARLIER VERSION OF THIS ENTRY CONCLUDED THE "
        "DIFFERENCES ARE MULTIPLICATIVE, on the grounds that over 358 paired "
        "samples the ratios held to 0.53% while the offsets wandered by 23%. "
        "That reasoning is withdrawn. It compares the RELATIVE spread of an "
        "offset (mean 0.29 V, so a small denominator) against that of a ratio "
        "(mean 1.02, so a denominator near one), which favours the ratio "
        "whatever the truth. Worse, the ratio's tightness is a mathematical "
        "CONSEQUENCE of a stable offset: if y = x + c then y/x = 1 + c/x, so "
        "the ratio's sd is forced to about sd(c)/mean(x). For volts against "
        "0142 that predicts 0.00447 and the observed value is 0.00460 -- "
        "agreement to 2.8%. The cited evidence for a multiplicative difference "
        "is what an additive one produces. Refitting each model by least "
        "squares on its own terms and comparing residuals in volts, across the "
        "whole corpus rather than 358 samples, the ADDITIVE model wins every "
        "pair: volts vs 0142 0.0591 against 0.0599 V (n=1691), volts vs 33E5 "
        "0.0617 against 0.0665 (n=4909), and 0142 vs 33E5 0.0330 against "
        "0.0412 (n=1691). Full linear fits give slopes 0.9985, 0.9893 and "
        "0.9739 with intercepts of +0.31, +0.90 and +0.79 V; a pure scaling "
        "would need an intercept near zero. So the three readings are three "
        "points on one rail separated by stable offsets, ordered volts > 0142 "
        "> 33E5 in every vehicle state. Two caveats kept deliberately: the "
        "0142-vs-33E5 ratio is 27% tighter than a pure offset predicts and "
        "that pair has the slope furthest from 1, so a small genuine gain "
        "difference between those two modules is not excluded; and both pairs "
        "involving volts sit near the 0.0289 V floor that 0.1 V quantisation "
        "imposes, so they cannot resolve the question on their own. Which "
        "reading is correct still needs a reference meter this project does "
        "not have, which is why this is 3 and not 4 -- but the disagreement is "
        "an offset between sense points, not the ADC gain error previously "
        "claimed. Note the resolutions differ structurally and 0142 is the "
        "instrument to use: 33E5 is ONE byte divided by 10, so 0.1 V steps and "
        "19 distinct values corpus-wide, while 0142 is two bytes divided by "
        "1000, giving 1 mV steps and 143 distinct values. The rail is also "
        "decoupled from traction load: over the 2026-09-04 drive 0142 "
        "correlates with pack current at only +0.10 across a 905 A swing, and "
        "regen and hard-draw samples differ by 0.008 V, a tenth of a pooled "
        "sd, ACROSS A SIGN REVERSAL that no monotonic ramp could fake"),

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
    "27BF": Evidence(2, _CB, ("parked", "driving", "charging"),
                     "regeneration-related candidate, and the 2026-09-04 drive "
                     "-- the first regen data in this corpus, 32 samples below "
                     "-1 A -- makes it a regen-specific accumulator. It is "
                     "monotonically non-decreasing and advances only when the "
                     "vehicle regenerates. It is separated from a general "
                     "charge counter by a clean negative: across 226 samples "
                     "spanning 4 h 39 min of AC charging, with pack current "
                     "negative throughout and SoC rising 69.943 -> 89.145, it "
                     "read exactly 77 in every one and never moved. So it is "
                     "not an unthresholded bidirectional coulomb counter. "
                     "It has a TICK THRESHOLD: intervals where it advances "
                     "carry median regen current 102.5 A against 26.1 A for "
                     "intervals where it does not (AUC 0.896). That "
                     "separation is real but not clean -- the 24.2-82.8 A band "
                     "contains 8 of 21 ticking and 17 of 30 non-ticking "
                     "intervals -- so 'ignores small regen' is the shape of "
                     "it, not a sharp cut. NO TICK SIZE IS ESTABLISHED and "
                     "none can be from 7-9 s polling: every candidate scaling "
                     "lands on a non-round divisor, and a decimation control "
                     "shows the apparent scale moving with sample rate, which "
                     "is what an artefact does. It also resets on a SHORTER "
                     "cycle than the charge and independently of its two "
                     "sibling counters, so the source's 'charge-cycle' label "
                     "is wrong about the cycle. 2 and not 3: the behaviour is "
                     "established, the units are not"),
    "27BB": Evidence(2, _CB, ("parked", "driving", "charging"),
                     "thermal-management energy candidate, and the accumulator "
                     "reading now survives the two hardest tests available. "
                     + ABA + "It runs 0 through the cold soak, 10 to 60 across "
                     "the first A/C phase, 70 to 110 across heat and 120 to "
                     "150 across the second A/C phase: monotonically "
                     "non-decreasing across a mode REVERSAL, in steps of 10, "
                     "from a zero start. It rose during A/C and would have "
                     "been published as an A/C response had the second A/C "
                     "phase not shown it climbing straight through. "
                     "It is not a clock. It froze for 5.18 h across 1307 "
                     "samples of charging, for 328 samples of Ready with HVAC "
                     "off, and for all 99 cold-soak samples. Its step rate "
                     "also differs about 2.8x between parked-with-HVAC and "
                     "driving, in the direction that excludes a per-poll "
                     "counter: the drive polled FASTER (7.3 s/sample against "
                     "9.5) and stepped SLOWER. "
                     "Its scope is since-last-charge, not lifetime. Exactly "
                     "one decrease exists in 3362 samples across 14 sessions, "
                     "830 -> 0, in the same poll that dist_since_chg_mi resets "
                     "47.72 -> 0.00 and 0x27B5 resets 177 -> 0, at the end of "
                     "a charge. Corpus-wide 3302 of 3302 decoded samples are "
                     "divisible by 10. "
                     "NO kWh-PER-COUNT SCALING IS CLAIMED. A candidate of 0.01 "
                     "kWh per count was fitted on parked windows and does not "
                     "survive: it is calibrated where HVAC is the dominant "
                     "load and does not hold once traction is, so it measures "
                     "that one regime rather than the field. What is "
                     "established is the shape -- a resettable, quantised, "
                     "load-driven accumulator -- not its units"),
    "27B5": Evidence(1, _CB, ("parked", "driving", "charging"),
                     "thermal-management distance candidate. It is NOT a "
                     "distance counter, and the 2026-09-04 data kills the "
                     "label twice over. Direct falsification: it advanced 32 "
                     "counts across 311 consecutive samples in which the "
                     "odometer did not move at all. A distance counter cannot "
                     "do that, and this is the clean test the project asked "
                     "for -- a counter that advances while the thing it "
                     "allegedly counts is stationary is not counting it. "
                     "Second, it carries no information its sibling does not: "
                     "it is reconstructible from 0x27BB to within +/-1 count "
                     "by any multiplier in roughly 0.2125-0.2143, so the two "
                     "are one signal at two scales. No round divisor fits that "
                     "range -- 1/5 = 0.2 and 1/4 = 0.25 are both rejected by "
                     "the residuals -- and the data does not pin it further, "
                     "so no scaling is claimed. It resets to zero in the same "
                     "poll as 0x27BB and dist_since_chg_mi, which bounds its "
                     "scope to since-last-charge. Kept at 1: what is "
                     "established here is what it is not, plus a redundancy"),
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
    "4149": Evidence(1, ("40",), ("parked", "driving", "charging"),
                     "EVSE advertised current candidate, and a lesson in "
                     "checking WHEN a value appeared. It held 0x00A0 = 160 "
                     "across all 147 samples of the 2026-09-04 charge while a "
                     "JuiceBox read 40.2 A twice, and 160/4 = 40.0 -- which "
                     "was written up as strong circumstantial support. It is "
                     "not. The value had already been 160 for 124 minutes "
                     "BEFORE the charger was connected, so the charge did not "
                     "produce it and the match is coincidence. "
                     "SHARPENED 2026-09-05, from closed sessions: the field "
                     "reads 388 in 384 samples taken while the vehicle is "
                     "MOVING, at 2 to 143 kph, across 5 distinct session "
                     "files, with the charger state 00 in every one. Nothing "
                     "is plugged into a vehicle doing 143 kph. That is a "
                     "direct falsification of the label rather than an "
                     "absence of support for it. "
                     "SECOND EVSE, 2026-09-05, PROVISIONAL -- these figures "
                     "come from a session still being written and must be "
                     "re-derived when it closes. A 12 A / 120 V cordset (Yura "
                     "91686-G5020, nameplate read by the owner) was connected "
                     "at 02:30:49. The charger state took 0x0C, a value absent "
                     "from all 7117 prior rows, and the charge settled at "
                     "about 0.51 kW into the pack by two agreeing routes. "
                     "0x4149 read 96 for the first 29 minutes, then settled "
                     "on 160: SEVENTEEN answers spanning 02:59:37 to 03:59:42, "
                     "every one of them 160, including isolated answers at "
                     "03:44:47 and 03:59:23 after a 35-minute gap in which "
                     "module 40 answered almost nothing -- the settled-charge "
                     "quiet already recorded here. A value that survives a "
                     "discontinuity like that is not a window artefact. So the "
                     "field reads 160 for a 40.2 A supply and 160 for a 12 A "
                     "one, and an advertised current cannot be equal for "
                     "sources differing 3.35x. That is a SECOND independent "
                     "falsification, agreeing with the moving-vehicle one and "
                     "reached from completely different data. "
                     "Two conclusions were drafted from this charge and "
                     "withdrawn before publication, both from reading a window "
                     "that ended before the value settled: first that 96 was "
                     "the 120 V charging value, then that the two chargers "
                     "needed different divisors (3.98 and 8.00) and that "
                     "960 x amps / volts fitted both. The settled reading of "
                     "160 kills all of it. Stored raw, level 1, no divisor "
                     "claimed, and a third EVSE at any other rating is what "
                     "would close the question"),
    "416C": Evidence(2, ("40",), ("parked", "driving", "charging"),
                     "the source calls this battery group voltage 1. It is "
                     "NOT a voltage of any group of this pack: across 2737 "
                     "paired samples it correlates with measured pack voltage "
                     "at +0.088, and the ratio between them spans 0.00 to "
                     "6.93. A per-group voltage has to track the pack it is "
                     "part of. Its two siblings undercut the label further -- "
                     "0x416D and 0x416E take only SIX distinct values each "
                     "corpus-wide and are effectively constant, which no live "
                     "voltage is. "
                     "What it does track is HVAC. " + ABA + "Across the "
                     "phases it reads a single value, 999, through all 99 "
                     "cold-soak samples; 2048-2544 across max A/C (60 "
                     "distinct); 664-794 across max heat (18 distinct); and "
                     "1789-2379 on the return to A/C. Cooling drives it UP and "
                     "heating drives it DOWN, with the quiescent value between "
                     "them -- the shape of a bidirectional actuator or valve "
                     "command rather than a temperature. The return to the "
                     "A/C band on the second A/C phase is what separates this "
                     "from elapsed time. "
                     "DO NOT read 999 as an HVAC-off signature. It held for "
                     "every sample of that one cold soak, but corpus-wide it "
                     "is only 119 of 2829 samples (4.2%), 878 is commoner at "
                     "286, and no session takes it as its only value. That is "
                     "the same shape as the 0x4127 = 1048 error published and "
                     "withdrawn on 2026-09-04 -- a value that fits one context "
                     "perfectly because the corpus for it was all one context. "
                     "What is established is the mode RESPONSE, not a lookup "
                     "from value to state. "
                     "324 distinct values over 0-2644, so it has the "
                     "granularity to carry a setpoint, but no setpoint test "
                     "has been run: every HVAC phase so far was at maximum, "
                     "where demand and setpoint are indistinguishable. No "
                     "scaling is claimed. 2 and not 3: one A-B-A, one vehicle, "
                     "and the units are unknown"),
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
    "4127": Evidence(3, ("40",), ("parked", "driving", "charging"),
                     "candidate labelled battery temperature A by the source. "
                     "It is NOT a temperature; it is a vehicle power-state "
                     "word, and a second drive on 2026-09-04 cross-validates "
                     "the useful half while refuting the specific half. "
                     "WHAT HOLDS, now on independent data: the values split "
                     "cleanly by whether a road speed is being reported at all. "
                     "Corpus-wide, 429 (9 samples) and 1048 (601 samples) occur "
                     "ONLY with no speed reported -- 610 of 610 -- while 234, "
                     "238, 242, 246, 261 and 601 occur with a speed in 2373 of "
                     "2374 samples. One sample of 234 breaks it. So the field "
                     "distinguishes powertrain down from powertrain up, and "
                     "{429, 1048} is the down family. "
                     "A CORRECTION within the hour: an earlier version of this "
                     "sentence said 'all 22 samples with no speed reported read "
                     "1048'. That was measured on a session file still being "
                     "written -- 93 rows of what became 335 -- and 429 simply "
                     "had not appeared yet. Re-run on the complete file, 67 of "
                     "69 no-speed samples read 1048 and 2 read 429. Every other "
                     "figure from that partial read did survive the complete "
                     "one unchanged, which was luck rather than method: a "
                     "session must be read after it closes. "
                     "WHAT DOES NOT: an earlier version of this entry said 246 "
                     "'appears in no other session and is first seen in the "
                     "exact poll of first wheel motion'. The second drive "
                     "holds 242 across all 68 moving samples and all 3 stopped "
                     "ones. So the powertrain-up value is a per-session "
                     "constant -- 246 in one drive, 242 in the next, both in "
                     "the 234/238/242/246 family -- and not a fixed motion "
                     "marker. The state distinction is between that family and "
                     "1048, not between particular members of it. "
                     "The earlier CORRECTION stands too: the claim that 1048 "
                     "'appears only while charging' was published from a MEAN "
                     "of -8.6 A and is false -- of 443 such samples carrying a "
                     "current, 184 are strictly positive with the charger "
                     "inactive. Charging is a subset of the no-speed state. "
                     + ABA + "In that experiment it held 234 through cold soak "
                     "and A/C, exactly 601 through all 44 heat samples, then "
                     "234 again, stepping within one poll of each switch. "
                     "3 and not 4: the no-speed rule is now cross-validated "
                     "across two drives and the parked corpus, but 601 still "
                     "rests on a single heat cycle"),
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
    "2429": Evidence(3, ("17",), ("parked", "driving", "charging"),
                     "the source calls this nominal (rated) pack voltage, /64. "
                     "It is a bipolar drive/regen torque signal zero-referenced "
                     "at 22534 (0x5806), and a second drive on 2026-09-04 "
                     "cross-validates it on data it was never fitted to. "
                     "NOT A VOLTAGE: in drive-20260904T020049Z.csv the measured "
                     "pack voltage swings 0.83 V -> 377.89 V across 526 samples "
                     "while this field reads 0x5806 in every one. It also moves "
                     "the wrong way under load -- at peak draw the real pack "
                     "voltage SAGS to 377.20 V while this field /64 RISES to "
                     "400.00 V, r = -0.554. "
                     "THE ZERO POINT, stated more carefully than before. An "
                     "earlier version said it is constant across 1083 "
                     "'stationary' samples. Those samples were all "
                     "powertrain-DOWN -- parked or charging -- and the second "
                     "drive shows why that matters: all 22 of its "
                     "no-speed-reported samples read exactly 22534, but the "
                     "three samples at speed 0 with the powertrain UP read "
                     "+435, -441 and +462 counts from it. Those are not noise. "
                     "The two positives sit at 100 and 700 kPa of brake with "
                     "the vehicle held at a stop, which is creep torque; the "
                     "negative sits at 1000 kPa with longitudinal_g at -0.134, "
                     "still decelerating. A stopped EV in Drive is not a "
                     "zero-torque EV, and the field knows it. That is "
                     "confirmatory rather than awkward -- a voltage cannot do "
                     "it -- but the claim is now 'zero when the powertrain is "
                     "down', not 'zero whenever stationary'. "
                     "SIGN CONVENTION, on the new drive: 31 of 32 samples above "
                     "+20 A read above the zero point and 28 of 29 below -20 A "
                     "read below it. "
                     "It is NOT a function of pack current -- gain falls from "
                     "18.2 counts/A at 30 kph to 3.9 at 132 kph, so the "
                     "published '~16.4 counts per amp' was an artefact of "
                     "pooling speeds. It tracks electrical power divided by "
                     "speed, R2 = 0.926 above 40 kph. Read that carefully: "
                     "hv_power_kw is pack_v x pack_a and pack_v varies only "
                     "+/-2.4%, so P/v is nearly current over speed, and calling "
                     "it TRACTIVE FORCE assumes roughly constant drivetrain "
                     "efficiency. THE INDEPENDENT ROUTE that agrees is the "
                     "accelerometer: using module 28 only, sharing no signal "
                     "with the module-17 power fit and using no pack current "
                     "at all, longitudinal_g and speed squared predict the "
                     "field at R2 = 0.766, with an implied vehicle mass of "
                     "3415 kg -- the right order for this vehicle. Two "
                     "different modules, two different physical quantities, "
                     "one answer. The second drive agrees again on the sign "
                     "convention and the zero point, against data the fit "
                     "never saw. "
                     "NO EQUATION IS SHIPPED. 2.30-2.42 newtons per count is "
                     "not a divisor a designer picks, and becomes round only by "
                     "assuming an unmeasured rolling radius or final drive. "
                     "Must be read as one big-endian u16: the low byte is "
                     "pinned at 0x06 in all 320 baseline samples. 3 and not 4 "
                     "because no scaling is established"),
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
