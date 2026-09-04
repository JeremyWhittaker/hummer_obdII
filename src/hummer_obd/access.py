"""What this node can reach, what it cannot, and how to check either answer.

Twenty documents in `docs/` describe this project, and not one of them answers
the question a new reader or a new agent actually arrives with: *what can this
thing see, and how would I confirm that myself?* Each document is organised by
**how something was discovered** -- a probe on a date, a research note, a
sourcing sweep -- which is the right shape for recording reasoning and the wrong
shape for looking something up. The answer to "can we read pack current" is
spread across four files, and on 2026-09-04 two of them still said no.

That is not a documentation problem to be solved by writing more documentation.
The project already learned this lesson twice: `registry.py` exists because a
hand-kept identifier list drifted thirty-six commits behind the code, and
`confidence.py` exists because the evidence grades drifted within hours of being
written. Both were fixed by rendering the answer from the thing that enforces it.

So this module renders the access matrix from the code that implements it:

* **what is collected** comes from :func:`hummer_obd.live.column_sources`, which
  already derives column -> module -> identifier from the recorder's own tables;
* **what may be transmitted** comes from putting a representative command to
  every gate and recording what each one says, rather than describing them;
* **what cannot be reached** is the one part that must be written by hand -- but
  it is written here as *data*, not as prose, so a test can hold it against
  :data:`hummer_obd.drive.COLUMNS` and fail when something on the list quietly
  becomes reachable.

That last point is the whole design. `docs/CAPABILITIES.md` claimed pack voltage
was unavailable while `pack_v` sat in `drive.COLUMNS`, and said so two hundred
lines above its own correction. A sentence cannot notice that. A table checked
against the recorder can.

Nothing here opens the serial device or touches the vehicle.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Optional

from . import drive
from .confidence import CONFIDENCE, LEVEL_NAMES, PRODUCTION_MINIMUM
from .live import column_sources
from .safety import (
    ALLOWED_OBD_MODES,
    FORBIDDEN_SERVICES,
    UnsafeCommandError,
    validate_command,
    validate_enhanced_command,
    validate_monitor_setup_command,
    validate_monitor_stream_command,
    validate_supervised_command,
)

__all__ = [
    "GATES",
    "GATE_PROBES",
    "UNREACHABLE",
    "REASONS",
    "Unreachable",
    "gate_matrix",
    "signal_rows",
    "render_matrix",
    "splice",
    "BEGIN_MARKER",
    "END_MARKER",
    "main",
]

BEGIN_MARKER = "<!-- BEGIN GENERATED ACCESS MATRIX -->"
END_MARKER = "<!-- END GENERATED ACCESS MATRIX -->"

DOC_PATH = "docs/ACCESS_MATRIX.md"


# -- the gates ----------------------------------------------------------------
#
# There are five, not four, and the difference between them is the safety model.
# Listing them in one place is the point: "the collector cannot send service 22"
# is a claim about which gate a caller was constructed with, and it should be
# readable as a row rather than traced through three files.

GATES: Final[tuple[tuple[str, Callable[[str], str], str], ...]] = (
    ("collector", validate_command,
     "The unattended gate. `hummer-collector` uses it, and it is the DEFAULT "
     "`SerialTransport` validator -- so a caller that forgets to choose gets "
     "this one, which is why it is the narrow one."),
    ("enhanced", validate_enhanced_command,
     "Supervised enhanced reads. Accepts service 22 for an exact enumerated "
     "identifier and nothing else; never guesses, increments or sweeps."),
    ("recorder", validate_supervised_command,
     "`hummer-drive`, the unattended session recorder: the union of collector "
     "and enhanced. It runs for hours with nobody watching, which is why every "
     "identifier it may send is enumerated."),
    ("monitor-setup", validate_monitor_setup_command,
     "`hummer-obd-passive` adapter configuration: the collector gate plus "
     "`STCMM0`, the receive-only CAN mode."),
    ("monitor-stream", validate_monitor_stream_command,
     "`hummer-obd-passive` streaming: `STMA` alone. Not even `STCMM0`."),
)

#: One command per class that matters, so the matrix shows the shape of the
#: policy rather than a sample of it.  Anything a reader might reasonably wonder
#: about should appear here; a class with no row is a question the matrix does
#: not answer.
GATE_PROBES: Final[tuple[tuple[str, str], ...]] = (
    ("010D", "service 01: legislated live data (vehicle speed)"),
    ("0142", "service 01: control module supply voltage"),
    ("0101", "service 01: monitor status, malfunction lamp and fault count"),
    ("0202", "service 02: freeze frame"),
    ("03", "service 03: read stored fault codes"),
    ("07", "service 07: read pending fault codes"),
    ("0A", "service 0A: read permanent fault codes"),
    ("0601", "service 06: on-board monitor results"),
    ("0902", "service 09: vehicle information (VIN)"),
    ("2227C6", "service 22: an ENUMERATED enhanced identifier (state of charge)"),
    # These two make the rule visible: 0x27C5 and 0x27C7 sit one step either
    # side of 0x27C6, which works.  One is refused and the other is accepted --
    # not because of where they sit, but because a source names one of them and
    # no source names the other.  Nearness is no evidence in either direction.
    ("2227C5", "service 22: one step BELOW an identifier that works"),
    ("2227C7", "service 22: one step ABOVE it -- and separately sourced"),
    ("22F190", "service 22: an identifier not in the enumerated set"),
    ("ATRV", "adapter: read connector voltage -- reaches no vehicle module"),
    ("ATCS", "adapter: CAN error counters"),
    ("ATSP7", "adapter: pin the protocol"),
    ("ATSP0", "adapter: auto-detect protocol -- detects BY TRANSMITTING"),
    ("STCMM0", "adapter: receive-only CAN monitoring, no acknowledgements"),
    ("STCMM1", "adapter: CAN monitoring as a normal node -- ACKs, so it transmits"),
    ("STMA", "adapter: start a monitor stream"),
    ("ATMA", "adapter: monitor all, the deprecated form"),
    ("04", "service 04: CLEAR FAULT CODES"),
    ("08", "service 08: on-board component control"),
    ("2E1234", "service 2E: WriteDataByIdentifier"),
    ("2701", "service 27: SecurityAccess"),
    ("3101FF00", "service 31: RoutineControl"),
    ("1101", "service 11: ECUReset"),
    ("3E00", "service 3E: TesterPresent"),
    ("2F1234", "service 2F: InputOutputControlByIdentifier"),
    ("1003", "service 10: DiagnosticSessionControl"),
    ("14FFFFFF", "service 14: ClearDiagnosticInformation"),
    ("2803", "service 28: CommunicationControl -- can silence a bus"),
    ("3400", "service 34: RequestDownload"),
    ("3500", "service 35: RequestUpload"),
    ("360001", "service 36: TransferData"),
    ("37", "service 37: RequestTransferExit"),
    ("3800", "service 38: RequestFileTransfer"),
    ("3B01", "service 3B: SAE J1979 legacy write"),
    ("3D01", "service 3D: WriteMemoryByAddress"),
    ("8301", "service 83: AccessTimingParameter"),
    ("8400", "service 84: SecuredDataTransmission"),
    ("8502", "service 85: ControlDTCSetting"),
    ("8701", "service 87: LinkControl"),
    ("010D;04", "injection: a legal read with a clear-codes smuggled behind it"),
    ("010D\r04", "injection: the same, separated by a carriage return"),
)


def gate_matrix() -> list[dict]:
    """Every probe put to every gate, with what each one actually said.

    Interrogating the gates rather than describing them is deliberate and is the
    same choice `capabilities.py` makes: a description can be written once and
    then be wrong, and this cannot.
    """
    rows: list[dict] = []
    for command, what in GATE_PROBES:
        verdicts: dict[str, bool] = {}
        for name, gate, _why in GATES:
            try:
                gate(command)
                verdicts[name] = True
            except UnsafeCommandError:
                verdicts[name] = False
            except Exception:  # a gate must refuse, never explode
                verdicts[name] = False
        rows.append({"command": command, "what": what, "gates": verdicts,
                     "accepted_by_any": any(verdicts.values())})
    return rows


# -- what is collected --------------------------------------------------------

def signal_rows() -> list[dict]:
    """Every recorded column, with where it comes from and what it is worth.

    Composed from `live.column_sources()` and `confidence.CONFIDENCE` rather
    than restated, so a column added to the recorder appears here without anyone
    remembering to add it.
    """
    sources = column_sources()
    priority_for: dict[str, str] = {}
    for group in drive.GROUPS:
        for did in group.dids:
            priority_for[f"0x{did}"] = group.priority.replace("ATCP", "0x")
    # Standard PIDs carry a priority too, and it is a different one -- read out
    # of STANDARD_ADDRESS rather than written down, because that tuple has
    # changed once already (a functional broadcast became a physically
    # addressed request) and a hardcoded label would have survived the change.
    standard_priority = next(
        (c.replace("ATCP", "0x") for c in drive.STANDARD_ADDRESS
         if c.startswith("ATCP")), "")
    for request, _column in drive.STANDARD_PIDS:
        priority_for[request] = standard_priority
    for column in drive.MONITOR_STATUS_COLUMNS:
        priority_for[drive.MONITOR_STATUS_PID] = standard_priority
    rows: list[dict] = []
    for column in drive.COLUMNS:
        where, identifier = sources.get(column, ("UNATTRIBUTED", ""))
        did = identifier[2:] if identifier.startswith("0x") else ""
        evidence = CONFIDENCE.get(did)
        rows.append({
            "column": column,
            "where": where,
            "identifier": identifier,
            "priority": priority_for.get(identifier, ""),
            "level": evidence.level if evidence else None,
            "level_name": LEVEL_NAMES[evidence.level] if evidence else "",
            "production": bool(evidence and evidence.level >= PRODUCTION_MINIMUM),
        })
    return rows


# -- what cannot be reached ---------------------------------------------------
#
# Hand-written, because it is the part that needs judgement -- but written as
# data so that `tests/test_access.py` can hold every entry against the
# recorder's own column list.  `docs/CAPABILITIES.md` claimed pack voltage was
# unavailable while `pack_v` was being written to every row, two hundred lines
# above its own correction.  A sentence cannot notice that; this can.

REASONS: Final[dict[str, str]] = {
    "forbidden": "Refused by the gate, permanently and by design. Not a "
                 "configuration option.",
    "unsourced": "No public source names an identifier for it. This project "
                 "adds an identifier only when a fetchable source names it "
                 "exactly, and never sweeps or guesses.",
    "hardware": "The adapter or the link physically cannot, whatever the "
                "software does.",
    "measured": "It was tried on this vehicle and it did not work. The "
                "measurement is the evidence.",
    "scope": "Deliberately out of scope for this repository.",
}


@dataclass(frozen=True)
class Unreachable:
    """One thing this node cannot get, and what would have to change."""

    name: str
    reason: str
    detail: str
    would_change_it: str
    #: A column name that must NOT exist in ``drive.COLUMNS`` while this entry
    #: stands.  Empty when the entry is not about a recordable signal (a
    #: forbidden command class, say).  A test checks every one.
    absent_column: str = ""
    #: What this project *does* have that is adjacent to the thing it cannot
    #: reach.  Required whenever ``detail`` cites an identifier this vehicle
    #: actually answers, and a test enforces that.
    #:
    #: The alternative -- forbidding the mention -- would be worse.  "We record
    #: 0x2AF1's twenty-four values and cannot say what they mean" is the honest
    #: and useful statement, and a rule that banned it would push authors toward
    #: vaguer prose rather than clearer.  So the rule is: name the adjacent
    #: thing you have, do not pretend it is not there.
    despite: str = ""


def _u(name, reason, detail, would_change_it, absent_column="", despite=""):
    return Unreachable(name=name, reason=reason, detail=detail,
                       would_change_it=would_change_it,
                       absent_column=absent_column, despite=despite)


#: Everything this node cannot reach. Listed after the reason table so the
#: categories read first: the *kind* of "no" matters more than the list.
UNREACHABLE: Final[tuple[Unreachable, ...]] = (

    # -- no public source names an identifier ---------------------------------
    _u("Individual cell voltages, all 96", "unsourced",
       "The pack's cells are readable only as an envelope. Two independent "
       "sourcing sweeps on 2026-09-04 found no identifier with a published "
       "scaling for per-cell voltage on any BT1 or BEV3/Ultium vehicle. The "
       "one real find -- 96 sequential identifiers 0x4181-0x419F and "
       "0x4200-0x4240 in the merged Chevrolet-Bolt-EV signalset, with byte-exact "
       "test vectors -- is 11-bit legacy addressing (headers 7E7/7EF) on the "
       "2017-2023 Bolt: a different addressing scheme and a different battery "
       "architecture, LG cell-monitoring units rather than Ultium modules. "
       "Honda Prologue is BEV3 and GM-built and names 168 per-cell voltages at "
       "0x2028, with no formula anywhere and on Honda's own address map.",
       "A fetchable source naming a per-cell identifier at a module this "
       "vehicle answers, with a scaling. Not a sweep: identifier guessing stays "
       "forbidden, and 0x27C5 being refused while 0x27C7 works is why.",
       absent_column="cell_01_v",
       despite="0x2AF5 gives cell average, minimum, maximum and spread, "
               "cross-validated against pack voltage at 96 cells in series."),

    _u("Per-cell and per-module temperatures", "unsourced",
       "0x2AF1 answers with twenty-four values -- the module count three "
       "independent structural results agree on -- and this project stores them "
       "raw and claims nothing. The source that supplied the identifier gives a "
       "single-byte formula, which cannot be right for a payload of that "
       "length; a raw ELM327 log from a GMC Sierra EV on wican-fw issue #497 "
       "shows the same identifier answering with 27 bytes, so an array is what "
       "a second BT1 vehicle sees too. No source names a scaling.",
       "A source naming the layout of 0x2AF1's payload, or enough thermal "
       "variation to decode it. The corpus now spans 23.4 F (91.4-114.8), up "
       "from 5.4 when that figure was first written -- but 0x2AF1's own rows "
       "cover only 9.0 F of it, because the identifier was added part-way "
       "through. Correlated against that 9 F, nothing in the payload resolves: "
       "the best any window reaches is +0.69, which is a direction and not a "
       "scaling. A cold morning would settle more than another source would.",
       despite="0x2AF1's twenty-four raw values are recorded every cycle, and "
               "0x0046 gives one pack temperature the vehicle holds."),

    _u("Motor speed (RPM)", "unsourced",
       "Two independent sweeps across eight OBDb vehicle repositories, "
       "meatpiHQ/wican-fw's profiles, issues and pull requests, and "
       "commaai/opendbc found no identifier for motor speed on any GM EV, at "
       "any module. The drive motor controllers 17, 1D and 1E answer four "
       "identifiers between them and none is a motor quantity.",
       "A fetchable source naming an identifier at module 17, 1D or 1E. The "
       "sweep is written up in SOURCING_2026-09-04.md so the next search can "
       "start from what was already covered rather than repeating it.",
       absent_column="motor_rpm",
       despite="Vehicle speed from legislated PID 010D, four wheel speeds from "
               "0x4A7A cross-validated against it at r=+0.997."),

    _u("Motor torque, requested and delivered", "unsourced",
       "Same sweep, same result. The OBDb Blazer EV and Silverado EV pull "
       "requests titled 'add torque units to signals schema' are a units-enum "
       "change, not a torque signal; no torque command exists in either "
       "repository.",
       "A fetchable source naming the identifier and its scaling.",
       absent_column="motor_torque_nm",
       despite="HV power from pack voltage times current, which bounds "
               "mechanical output but does not resolve it per motor."),

    _u("Inverter and stator temperature", "unsourced",
       "Nothing found on modules 17, 1D or 1E in any source. The wican-fw "
       "issue #884 author mentions having 'motor temps, current' candidates "
       "that are not fully confirmed -- and gives no identifier numbers, with "
       "zero comments and no follow-up pull request, so there is nothing "
       "extractable.",
       "The #884 author publishing the numbers, or any source naming them.",
       absent_column="inverter_temp_c"),

    _u("Propulsion and regen power limits", "unsourced",
       "The vehicle certainly computes both -- they bound what the accelerator "
       "can request -- and no public source names an identifier for either.",
       "A fetchable source. These would be genuinely valuable: a regen limit "
       "explains braking behaviour that the recorded data currently cannot."),

    _u("Motor phase current, DC link voltage", "unsourced",
       "No identifier found. 0x2414 is pack current at module 17, which the "
       "source describes as HV battery current from the drive-motor controller "
       "-- not a phase current, and this project does not present it as one.",
       "A source naming a phase-current identifier.",
       despite="0x2414 pack current, level 4, cross-validated against the "
               "energy field's slope both charging and discharging."),

    _u("DC-DC converter output current", "unsourced",
       "Nothing in any source reviewed, on any platform. This is the one that "
       "would settle an open question: three readings of the 12 V rail differ "
       "multiplicatively by 2.4% and 5.9%, and the load test that would "
       "distinguish a wiring drop from a calibration difference has to use "
       "traction power as a proxy because 12 V current is not measured at all.",
       "A source naming a DC-DC current identifier, or a clamp meter on the "
       "12 V feed -- which is hardware, not software.",
       despite="Three independent 12 V readings: ATRV at the connector, PID "
               "0142 at module 17, and 0x33E5 at the motor controllers."),

    _u("12 V battery state of charge", "unsourced",
       "Voltage is available three ways; state of charge, as a distinct "
       "quantity, is not named by any source reviewed.",
       "A source naming it. Note that 12 V SoC is generally computed rather "
       "than measured, so it may simply not exist as a diagnostic identifier.",
       despite="12 V terminal voltage, and a 6.8-hour sleep trace showing the "
               "rail settling at 12.80 V with zero CAN traffic."),

    _u("Charger AC voltage, AC current, DC current", "measured",
       "This is the strongest negative in the list, because it is not merely "
       "unsourced. The legacy Bolt signalset carries all of them, merged and "
       "byte-exact (0x4368, 0x4369, 0x436B, 0x436C, 0x4373, 0x4531). They were "
       "tried on a real 2025 Cadillac LYRIQ -- BEV3, one platform generation "
       "closer to this truck than the Bolt -- and returned NRC 0x31 "
       "requestOutOfRange in every vehicle mode. The LYRIQ repository's own CI "
       "probe independently records the same set under "
       "unsupported_commands_by_ecu.",
       "A BT1-specific source, or a willingness to send identifiers that a "
       "closer platform has already refused. Sourced-and-measured-negative is "
       "a stronger reason to decline than merely unsourced.",
       absent_column="charge_ac_amps",
       despite="Charge power derived from the 0x27AF energy slope over a "
               "60-second window, validated against a real AC session at "
               "7.81 kW, and pack DC power from voltage times current."),

    _u("Chiller state, heater state, cabin HVAC power draw", "unsourced",
       "Nothing with a formula in any source reviewed. The one lead, "
       "HONDA_AC_P at 0x2613 on the Prologue, is a name whose meaning the "
       "source itself does not state, on Honda's address map.",
       "A source naming any of them at a module this vehicle answers.",
       despite="0x2709 is an A/C compressor temperature candidate that answers "
               "here and is stored raw; 0x27BB and 0x27B5 are "
               "thermal-management candidates in the same position."),

    _u("Tyre pressures, door and lock state, exterior lighting", "unsourced",
       "Body-domain signals. Module 40 BCM-BodyControl answers nine "
       "identifiers, all of them battery- and charging-related from the LYRIQ "
       "source, and its own service 01 support bitmap advertises just two PIDs. "
       "No source names a body identifier for this platform, and none has been "
       "asked for.",
       "A source naming body identifiers at module 40. The addressing is "
       "already proven, so only the identifiers are in question -- which makes "
       "this one of the cheaper gaps to close if a source appears."),

    _u("Anything at module CD, the second battery manager", "measured",
       "CD is present and speaks service 22: it returns a well-formed "
       "7F 22 31 requestOutOfRange, not silence. Seventeen identifiers were put "
       "to it at both priorities -- including the four ISO 14229-1 standard "
       "identification identifiers and every identifier proven at its sibling "
       "CB -- and it refused all of them. A module that declines the standard's "
       "own identification identifiers is not hiding a namespace behind "
       "something nobody has guessed; it exposes nothing in the session it "
       "answers in.",
       "A different diagnostic session, which is service 0x10 and permanently "
       "forbidden here. CD is closed from this access path, and that is a "
       "statement about the path rather than about the module.",
       despite="Its sibling CB answers thirteen identifiers, including "
               "everything the battery telemetry depends on."),

    _u("Anything at module 45, the gateway", "unsourced",
       "This vehicle names 45 as 'Gateway Module - GWM' in its own service 09 "
       "census, and it answers services 01 and 09 there. Four ISO standard "
       "identification identifiers are the only service 22 requests ever put to "
       "it; all four returned 7F 22 31. No source names a GM gateway "
       "identifier.",
       "A source naming one. Note the gateway is the component most likely to "
       "hold routing and network state, and equally the one least likely to be "
       "publicly documented."),

    # -- the adapter or the link physically cannot ----------------------------
    _u("CAN FD frames", "hardware",
       "The OBDLink MX+ implements Classical CAN only. No software change "
       "reaches an FD segment through it.",
       "An isolated CAN FD interface AND service information identifying which "
       "internal pair carries what, at which bitrate. The hard rule is in "
       "CAN_FD_EXPANSION.md: never connect anything to an internal pair until "
       "the bus and bitrate are identified from service information or measured "
       "physical-layer evidence. A wrong bitrate does not fail politely."),

    _u("Bus load, as a measurement", "hardware",
       "Frame counts from this connector are not a measurement of bus load and "
       "the passive tool says so in its own output. The link is ASCII over "
       "Bluetooth RFCOMM at 115200 baud, which caps at a few hundred frames per "
       "second against a bus carrying thousands. The capture is lossy by "
       "construction and the loss is not recorded anywhere.",
       "A hardware CAN interface with an accurate timestamping receiver. Not "
       "reachable through an ELM-class adapter at any baud rate."),

    _u("The vehicle's internal networks", "hardware",
       "The diagnostic connector is the gateway's outside. Everything this "
       "project has ever obtained came from asking a module through it. What "
       "happens on the internal buses is not visible from here, and the one "
       "public GM pack decode (gm_global_a_high_voltage_management.dbc) came "
       "from a tap behind the forward camera on the previous-generation "
       "platform, not from a diagnostic connector.",
       "A physical tap, which needs the hardware and the identification in "
       "CAN_FD_EXPANSION.md, and is explicitly not authorised by this "
       "repository."),

    _u("GPS or location", "hardware",
       "There is no GPS receiver on the node, and location lives in the "
       "OnStar/VCIM telematics domain rather than on any diagnostic path. No "
       "GPS signal appears in any GM Global-A, Rivian or Tesla Model 3 DBC. "
       "This is an argument rather than a measurement -- it was not tested "
       "against this vehicle -- and it is offered as a reason not to plan on "
       "GPS, not as proof it is absent.",
       "A GPS receiver on the node, which is hardware and would also change "
       "what the session files contain. Session CSVs deliberately carry no "
       "location today."),

    # -- measured negative on this vehicle ------------------------------------
    _u("Passive broadcast traffic at the connector", "measured",
       "Measured directly rather than argued: 30.1 seconds of receive-only CAN "
       "monitoring on 2026-09-04, parked and awake, returned ZERO BYTES. The "
       "adapter did not even assert the acknowledgement bit a normal CAN node "
       "puts on every frame it hears; 65 bytes of adapter configuration went "
       "out and nothing came back. CAN error counters read T:00 R:00 before and "
       "after, DTC inventory unchanged.",
       "Nothing, on this access path. The gateway forwards nothing unsolicited "
       "to pins 6 and 14, and no adapter changes what a gateway chooses to "
       "forward. Driving and charging were not tested, so that much remains "
       "open -- but the state easiest to imagine being chatty was the one "
       "measured.",
       despite="Every byte this project has ever obtained, all of it by asking."),

    _u("Freeze frame data", "measured",
       "Service 02 is permitted and proven to work. There is no frame to read: "
       "a freeze frame exists only alongside a stored fault, and this vehicle "
       "reports none -- services 03, 07 and 0A all return no codes, verified "
       "either side of the passive capture on 2026-09-04.",
       "A fault occurring. This is a capability that is present and has nothing "
       "to show, which is a different thing from a capability that is absent.",
       despite="PID 0101 now records the malfunction lamp and stored-fault "
               "count in every row, so a fault appearing mid-drive is caught "
               "with the speed and distance either side of it."),

    _u("On-board monitor results (service 06)", "measured",
       "The service is permitted and proven. The vehicle advertises ZERO "
       "monitor IDs, so there is nothing to return. An EV with no combustion "
       "emissions monitors is the expected shape of that answer.",
       "Nothing. This is the vehicle correctly reporting that it runs no such "
       "monitors."),

    # -- forbidden, permanently -----------------------------------------------
    _u("Clearing fault codes (service 04)", "forbidden",
       "Refused by every gate. Not a configuration option, and an import-time "
       "assertion fails the build if service 04 is added to the allowed set.",
       "Nothing within this repository. Clearing codes destroys evidence and is "
       "outside a read-only telemetry node's remit by design."),

    _u("Writing, controlling, resetting or unlocking anything", "forbidden",
       "Every UDS write, control, security, reset and routine service is in "
       "FORBIDDEN_SERVICES: 04, 08, 10, 11, 14, 27, 28, 2E, 2F, 31, 34, 35, 36, "
       "37, 38, 3B, 3D, 3E, 83, 84, 85 and 87. The node is structurally "
       "incapable of transmitting a command in the imperative sense, and the "
       "gate matrix above shows each one refused by all five gates rather than "
       "asserting it.",
       "Nothing. This is the invariant the whole project is built around, and "
       "it is not a tunable."),

    _u("Remote commands: lock, unlock, precondition, remote start", "forbidden",
       "These are not diagnostic operations at all -- they belong to the "
       "telematics domain -- and even if they were reachable, every service "
       "that could express them is forbidden.",
       "Nothing here. See the OnStar row for where such a thing would have to "
       "live."),

    # -- out of scope by decision ---------------------------------------------
    _u("OnStar and GM cloud data", "scope",
       "Reachable in principle with GM account credentials, and deliberately "
       "not reached. It is a different trust domain from a read-only OBD "
       "reader: it needs stored credentials, it is bidirectional by design, and "
       "putting it here would replace a structural safety model with 'be "
       "careful'.",
       "A separate repository with isolated credentials and its own command "
       "allowlist. The decision to keep this repo read-only was made "
       "deliberately, not by omission."),

    _u("Dealer-level diagnostics (MDI2 + GDS2)", "scope",
       "The only route that would definitively read everything, because it is "
       "what the dealer uses and it authenticates. It is subscription-priced, "
       "Windows-only, and -- decisively -- a bidirectional tool whose entire "
       "value is that it can command the vehicle.",
       "A separate machine and a human driving it. Introducing it here would "
       "not extend this project; it would replace its safety model."),

    _u("The VIN, in committed evidence", "scope",
       "The vehicle answers service 09 item 02 and the VIN is readable. It is "
       "deliberately kept out of committed artefacts: raw transcripts are "
       "gitignored, evidence JSON is gitignored, session CSVs carry no identity "
       "columns, and F190 was excluded from the ISO identifier set for exactly "
       "this reason.",
       "Nothing that should change. The capability exists; the choice not to "
       "publish it is the point.",
       despite="Service 09 works and the probe reads it; capabilities reports "
               "mask it."),
)


# -- rendering ----------------------------------------------------------------

#: Characters that would break a markdown table if written literally, rendered
#: as their source escape instead.  The carriage return is not hypothetical:
#: one gate probe *is* a carriage-return-separated command batch, because that
#: is a real injection attempt the gate has to refuse, and writing it raw split
#: its own table row in half and made the document fail its own idempotency
#: check.  Same class of defect as an unescaped pipe in a provenance string,
#: which this project also shipped once.
_ESCAPES: Final[dict[str, str]] = {
    "\\": "\\\\", "\r": "\\r", "\n": "\\n", "\t": "\\t", "|": "\\|",
}


def _literal(value: str) -> str:
    """*value* rendered so a markdown table survives it, losing nothing."""
    out = []
    for char in str(value):
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return "".join(out)


def _cell(value: str) -> str:
    """Prose for a table cell: collapsed whitespace, escaped delimiters."""
    return _literal(" ".join(str(value).split()))


def render_matrix() -> str:
    """The generated block: gates, signals and the unreachable list."""
    out: list[str] = [BEGIN_MARKER, "",
                      "<!-- Generated by hummer_obd.access.render_matrix().",
                      "     Do not edit by hand: tests/test_access.py fails when",
                      "     this drifts from the code. Regenerate with",
                      "       PYTHONPATH=src python3 -m hummer_obd.access -->",
                      ""]

    # -- the gates ------------------------------------------------------------
    out += ["## 1. What may be transmitted", "",
            "Five gates, not one. Which gate a caller was built with *is* the "
            "safety model, so it is shown as a table rather than described.", ""]
    for name, _gate, why in GATES:
        out.append(f"* **`{name}`** — {why}")
    out += ["",
            "Every cell below is produced by putting that command to that gate "
            "and recording what it said. Nothing here is asserted.", "",
            "| Command | What it is | " + " | ".join(f"`{n}`" for n, _, _ in GATES) + " |",
            "|---|---|" + "---|" * len(GATES)]
    for row in gate_matrix():
        cells = " | ".join("**yes**" if row["gates"][n] else "no" for n, _, _ in GATES)
        out.append(f"| `{_literal(row['command'])}` | "
                   f"{_cell(row['what'])} | {cells} |")

    out += ["",
            f"**{len(ALLOWED_OBD_MODES)} OBD services** are permitted at all: "
            + ", ".join(f"`{m}`" for m in sorted(ALLOWED_OBD_MODES)) + ". "
            f"**{len(FORBIDDEN_SERVICES)} services are permanently forbidden** "
            "and an import-time assertion fails the build if one is added to "
            "the allowed set: "
            + ", ".join(f"`{s}`" for s in sorted(FORBIDDEN_SERVICES)) + ".", ""]

    # -- the signals ----------------------------------------------------------
    rows = signal_rows()
    production = [r for r in rows if r["production"]]
    out += ["## 2. What is collected", "",
            f"**{len(rows)} columns** per sample. The level is from "
            "`hummer_obd.confidence`; **only level 3 and above is a production "
            "telemetry reading**, and "
            f"**{len(production)} of {len(rows)}** columns clear that bar. "
            "Everything below it is either raw evidence being accumulated or a "
            "candidate waiting for the vehicle state that will decide it.", "",
            "| Column | Where it comes from | Identifier | Priority | Level |",
            "|---|---|---|---|---|"]
    for r in rows:
        level = "—" if r["level"] is None else f"**{r['level']}** {r['level_name']}"
        out.append(f"| `{r['column']}` | {_cell(r['where'])} | "
                   f"{('`' + r['identifier'] + '`') if r['identifier'] else '—'} | "
                   f"{r['priority'] or '—'} | {level} |")

    # -- the unreachable ------------------------------------------------------
    out += ["", "## 3. What cannot be reached", "",
            "The kind of \"no\" matters more than the list, so it is stated "
            "first. These are not interchangeable:", ""]
    for key, meaning in REASONS.items():
        out.append(f"* **{key}** — {meaning}")
    out += ["", "| Cannot reach | Why | Detail | What we do have | What would change it |",
            "|---|---|---|---|---|"]
    for item in UNREACHABLE:
        out.append(f"| {_cell(item.name)} | **{item.reason}** | "
                   f"{_cell(item.detail)} | {_cell(item.despite) or '—'} | "
                   f"{_cell(item.would_change_it)} |")

    out += ["", END_MARKER]
    return "\n".join(out)


def splice(document: str) -> str:
    """*document* with its generated block replaced by a fresh one."""
    start = document.find(BEGIN_MARKER)
    end = document.find(END_MARKER)
    if start == -1 or end == -1:
        raise ValueError(
            "the document has no generated-matrix markers; add "
            f"{BEGIN_MARKER} and {END_MARKER} where the tables belong"
        )
    if end < start:
        raise ValueError("the matrix markers are in the wrong order")
    return document[:start] + render_matrix() + document[end + len(END_MARKER):]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the access matrix from the code that enforces it. "
                    "Offline: opens no serial device and touches no vehicle."
    )
    parser.add_argument("--doc", default=DOC_PATH,
                        help=f"document to splice (default {DOC_PATH})")
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the document is out of date")
    parser.add_argument("--json", dest="json_path",
                        help="write the matrix as JSON here")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="print the rendered block instead of splicing")
    args = parser.parse_args(argv)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({
                "gates": [{"name": n, "purpose": w} for n, _g, w in GATES],
                "gate_matrix": gate_matrix(),
                "signals": signal_rows(),
                "allowed_obd_modes": sorted(ALLOWED_OBD_MODES),
                "forbidden_services": sorted(FORBIDDEN_SERVICES),
                "unreachable": [
                    {"name": u.name, "reason": u.reason, "detail": u.detail,
                     "would_change_it": u.would_change_it,
                     "absent_column": u.absent_column}
                    for u in UNREACHABLE
                ],
            }, handle, indent=2, sort_keys=True)

    if args.show:
        print(render_matrix())
        return 0

    path = Path(args.doc)
    if not path.exists():
        print(f"{path} does not exist", file=sys.stderr)
        return 2
    document = path.read_text(encoding="utf-8")
    try:
        updated = splice(document)
    except ValueError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 2
    if args.check:
        if updated != document:
            print(f"{path} is out of date; run this without --check",
                  file=sys.stderr)
            return 1
        print(f"{path} is current")
        return 0
    if updated != document:
        path.write_text(updated, encoding="utf-8")
        print(f"{path} updated ({len(signal_rows())} columns, "
              f"{len(GATE_PROBES)} gate probes, {len(UNREACHABLE)} limits)")
    else:
        print(f"{path} was already current")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
