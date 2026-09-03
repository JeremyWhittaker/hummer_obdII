"""Supervised enhanced-diagnostics reads (UDS service ``22``).

Everything else in this project is unattended-safe: the collector can run for
hours without a person present because :func:`safety.validate_command` refuses
anything that is not a standard read.  This module is the deliberate exception,
and it is built so that the exception cannot leak.

Three properties make that true:

1. **A different gate.**  Requests here go through
   :func:`safety.validate_enhanced_command`, which accepts service ``22`` only
   for an identifier enumerated in :data:`safety.ENHANCED_READ_DIDS`.  The
   collector calls ``validate_command``, which refuses service ``22`` outright,
   so nothing that runs unattended can reach this path.
2. **Transmission is opt-in per run.**  Without ``--confirm`` the tool prints
   the exact byte sequence it *would* send and exits without opening the serial
   device.  A dry run is the default because the interesting failure mode is a
   person running this on the wrong vehicle, not a bug.
3. **One request per identifier, per run.**  There is no loop and no sweep.

Why the raw response matters more than the decoded value
--------------------------------------------------------
The published community profile this module implements changed its own byte
offsets after release -- the state-of-charge field moved from ``B4:B5`` to
``B8:B9``.  That is a good reason to record what the truck actually said and
work out the offset here, rather than to trust an equation and store a number
that looks plausible.  So a run reports:

* the complete raw reply text, untouched,
* every frame with the identifier that carried it,
* and, for a positive response, *every* two-byte window with several candidate
  scalings applied -- so an operator comparing against the dashboard can see
  which window tracks the real value instead of assuming one.

A negative response is just as informative, and the four codes mean different
things that must not be collapsed into "it didn't work":

======  ==============================  ====================================
NRC     Name                            What it tells us
======  ==============================  ====================================
``11``  serviceNotSupported             the ECU has no service 22 at all
``22``  conditionsNotCorrect            right identifier, wrong vehicle state
``31``  requestOutOfRange               service 22 works; this DID does not
``33``  securityAccessDenied            it exists and is protected
``34``  authenticationRequired          it exists and needs Global B auth
======  ==============================  ====================================

``31`` in particular is a *success* for the method even though it is a failure
for the identifier: it proves the module answered a service 22 request.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .decode import AdapterReply, negative_response_name, parse_reply
from .rawlog import RawLog
from .safety import (
    ENHANCED_READ_DIDS,
    UnsafeCommandError,
    validate_command,
    validate_enhanced_command,
)
from .transport import SerialTransport, Transport, TransportError

__all__ = [
    "EnhancedProfile",
    "PROFILES",
    "EnhancedResult",
    "run_profile",
    "candidate_scalings",
    "candidate_triples",
    "main",
]


@dataclass(frozen=True)
class EnhancedProfile:
    """One supervised experiment: how to address a module and what to ask it.

    ``init`` is applied in order before the request.  Every entry is validated
    against the ordinary adapter allowlist, so a profile cannot introduce a new
    kind of command by being added here -- only a new *combination* of
    already-approved ones.
    """

    key: str
    description: str
    #: Where the identifier and addressing came from.  A profile without a real
    #: source has no business existing; this string ends up in the evidence
    #: file so a reader can check the claim rather than take it on trust.
    provenance: str
    init: tuple[str, ...]
    #: ``(request, signal name, published decoder description)``.
    requests: tuple[tuple[str, str, str], ...]
    #: Documented request/response CAN identifiers, recorded for the evidence
    #: file.  The adapter derives these from ``init``; these are the human
    #: readable form.
    tx_id: str = ""
    rx_id: str = ""


#: The GM BT1/BEV3 profile.
#:
#: Transcribed from ``vehicle_profiles/bt1/bt1.json`` in ``meatpiHQ/wican-fw``,
#: whose ``car_model`` field names the Hummer EV explicitly.  The init string
#: there is a single semicolon-separated line; it is split here because this
#: project's safety gate refuses batched commands on principle, so each one is
#: validated and sent on its own.
#:
#: ``ATCP14`` is load-bearing and easy to miss: ``ATSH`` carries only the low
#: three bytes of a 29-bit identifier, so without the priority byte the request
#: goes out as ``0x18DACBF1`` rather than ``0x14DACBF1`` and the module does not
#: answer.
BT1 = EnhancedProfile(
    key="bt1",
    description="GM BT1/BEV3 (Hummer EV, Silverado EV, Sierra EV, Lyriq, Blazer EV, ...)",
    provenance=(
        "addressing and 0x27C6 from meatpiHQ/wican-fw vehicle_profiles/bt1/"
        "bt1.json (sha256 26dc621a...), whose car_model reads "
        '"BT1: Hummer EV, Silverado EV, Sierra AV; BEV3: Cadillac Lyriq, ..."; '
        "the five further identifiers from vehicle_profiles/gmc/sierra-ev.json "
        "(sha256 19ca7a20...), same module and same request/response "
        "identifiers, BT1 platform family. Both fetched 2026-09-02 from "
        "raw.githubusercontent.com"
    ),
    init=(
        "ATZ",
        "ATE0",
        "ATL0",
        "ATS0",
        "ATH1",       # keep headers: we must know which module answered
        "ATAL",       # allow long messages; a 22 reply can exceed 7 bytes
        "ATSP7",      # ISO 15765-4, 29-bit, 500 kbit/s
        "ATCP14",     # CAN priority 0x14 -> request goes out as 0x14DACBF1
        "ATSHDACBF1",  # target module CB, tester F1
        "ATCRA142AF1CB",  # accept only that module's reply
        "ATFCSH14DACBF1",  # flow control uses the same request identifier
        "ATFCSD300000",    # clear to send, block size 0, no separation time
        "ATFCSM1",         # use the flow control values set above
        "ATST96",          # response timeout, from the published profile
    ),
    # The addressing comes from bt1.json, which is the profile that names the
    # Hummer EV and which selects protocol 7 explicitly.  The additional
    # identifiers come from sierra-ev.json, which targets the *same* module at
    # the same request and response identifiers -- and the Sierra EV is BT1,
    # the platform family bt1.json itself groups with the Hummer.  Taking the
    # headers from one and the identifiers from the other is deliberate:
    # sierra-ev.json opens with ``ATSP6`` (11-bit) while using a 29-bit header,
    # which cannot be right for this vehicle, and bt1.json's ``ATSP7`` is.
    #
    # The two profiles state different byte offsets for the identifier they
    # share, and the difference is exactly four -- the length of the CAN
    # header.  bt1.json counts from the start of the whole frame, sierra-ev.json
    # from the ISO-TP PCI byte.  Both land on the same bytes, which this
    # project confirmed against a real captured frame rather than guessing at.
    requests=(
        ("2227C6", "hv_battery_soc",
         "SOC = [B8:B9]/655.35 (bt1) = [B4:B5]/655.35 (sierra-ev); percent"),
        ("2227AF", "hv_energy_remaining",
         "HV_CAPACITY_R = [B4:B5]/100 (sierra-ev)"),
        ("2227C7", "range",
         "RANGE = [B4:B6]/103 (sierra-ev); three bytes"),
        ("2227C0", "distance_since_full_charge",
         "DIST_SINCE_FULL_CHARGE = [B4:B6]/16.09344 (sierra-ev); three bytes"),
        ("220046", "temperature",
         "TMP_A = (B4-40)*1.8+32 (sierra-ev); single byte, Fahrenheit"),
        ("225401", "charger_dc_power",
         "CHARGER_DC_PWR = [B4:B5]/4350 (sierra-ev)"),
    ),
    tx_id="0x14DACBF1",
    rx_id="0x142AF1CB",
)

#: BEV3 identifiers tried against the same battery manager.
#:
#: A weaker claim than :data:`BT1` and labelled as such: these come from a
#: Chevrolet Equinox EV signalset, which is BEV3 rather than BT1.  They are
#: worth one supervised attempt because they address the same module this
#: vehicle has already named for itself, and because the same file's entry for
#: 0x27C6 is arithmetically identical to the scaling this vehicle has already
#: confirmed.  ``7F 22 31`` is the expected failure and is a perfectly good
#: result: it would say the module serves service 22 but not these identifiers.
BEV3_BSM = EnhancedProfile(
    key="bev3-bsm",
    description="BEV3 identifiers against the BT1 battery system manager (CB)",
    provenance=(
        "OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, fetched "
        "2026-09-02 from raw.githubusercontent.com; hdr DACB. BEV3, not BT1 -- "
        "unproven on this vehicle"
    ),
    init=BT1.init,
    requests=(
        ("222AF5", "cell_voltage_avg_min_max",
         "three 16-bit fields / 10000 -> volts (OBDb Equinox EV)"),
        ("222B43", "hv_battery_soc_8bit",
         "byte * 100 / 255 -> percent (OBDb Equinox EV)"),
    ),
    tx_id="0x14DACBF1",
    rx_id="0x142AF1CB",
)

#: The same idea aimed at drive motor controller 2, which this vehicle names.
BEV3_DMCM = EnhancedProfile(
    key="bev3-dmcm",
    description="BEV3 drive-motor identifier against DMC2 (1D)",
    provenance=(
        "OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json, hdr DA1D; this "
        "vehicle names address 1D as DMC2-DriveMotorCtrl2. BEV3, not BT1 -- "
        "unproven on this vehicle"
    ),
    init=(
        "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
        "ATSP7",
        "ATCP14",
        "ATSHDA1DF1",       # target module 1D, tester F1
        "ATCRA142AF11D",    # that module's reply
        "ATFCSH14DA1DF1",
        "ATFCSD300000",
        "ATFCSM1",
        "ATST96",
    ),
    requests=(
        ("2233E5", "dmcm_battery_voltage",
         "byte / 10 -> volts (OBDb Equinox EV)"),
    ),
    tx_id="0x14DA1DF1",
    rx_id="0x142AF11D",
)

#: Chassis dynamics from the brake system controller.
#:
#: Address 28 is ``BSCM-BrakeSystem``, which this vehicle named for itself.
#: The identifiers and their scalings come from OBDb/Cadillac-LYRIQ test
#: fixtures, which pair a captured response with its expected decoded value --
#: so every formula here was checked against the vectors arithmetically rather
#: than believed.  LYRIQ is BEV3 rather than BT1, but the same fixture
#: directory's three battery identifiers all answered on this truck before
#: these were tried.
CHASSIS_BSCM = EnhancedProfile(
    key="chassis-bscm",
    description="chassis dynamics from the brake system controller (28)",
    provenance=(
        "OBDb/Cadillac-LYRIQ tests/test_cases/2024/commands/DA28.*.yaml, "
        "fetched 2026-09-02; scalings derived from the captured "
        "response/expected-value pairs and verified against every vector. "
        "BEV3 source, unproven on this vehicle"
    ),
    init=(
        "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
        "ATSP7",
        "ATCP14",
        "ATSHDA28F1",      # target BSCM, tester F1
        "ATCRA142AF128",   # that module's reply
        "ATFCSH14DA28F1",
        "ATFCSD300000",
        "ATFCSM1",
        "ATST96",
    ),
    requests=(
        ("224A7A", "wheel_speed_fl_fr_rl_rr", "one byte per wheel, km/h"),
        ("224A7C", "brake_pressure", "(B0 - 10) * 100, kPa"),
        ("224C2D", "steering_angle", "signed 16-bit * 0.022, degrees"),
        ("224C2F", "lateral_g", "signed 16-bit * 0.0015928, g"),
        ("224C30", "longitudinal_g", "signed 16-bit * 0.0015928, g"),
    ),
    tx_id="0x14DA28F1",
    rx_id="0x142AF128",
)

def _module_profile(key, ecu, description, provenance, requests):
    """A profile aimed at one module address, reusing BT1's session setup.

    Only the three addressing commands differ between modules, so building
    these from a template keeps the difference visible instead of burying it in
    fourteen near-identical lines each.
    """
    return EnhancedProfile(
        key=key,
        description=description,
        provenance=provenance,
        init=(
            "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
            "ATSP7", "ATCP14",
            f"ATSHDA{ecu}F1",
            f"ATCRA142AF1{ecu}",
            f"ATFCSH14DA{ecu}F1",
            "ATFCSD300000", "ATFCSM1", "ATST96",
        ),
        requests=requests,
        tx_id=f"0x14DA{ecu}F1",
        rx_id=f"0x142AF1{ecu}",
    )


#: The same drive-motor identifier, aimed at the two sibling controllers.
#:
#: This vehicle named three drive motor controllers for itself -- 17 DMCM,
#: 1D DMC2, 1E DMC3 -- and only 1D has ever been asked anything.  The source
#: names the identifier for "DMCM battery voltage", which is the family all
#: three belong to, so pointing it at the other two is directing a *sourced*
#: identifier at modules the vehicle itself named.  That is not the same as
#: guessing an identifier, and a 7F 22 31 answers the question harmlessly.
_DMC_PROVENANCE = (
    "OBDb/Chevrolet-Equinox-EV signalsets/v3/default.json DID 2233E5, "
    "'DMCM battery voltage', byte/10 volts. Proven on this vehicle at module "
    "1D; aimed here at the sibling controllers this vehicle also named"
)
DMC_17 = _module_profile(
    "dmc-17", "17", "drive motor controller 1 (DMCM)", _DMC_PROVENANCE,
    (("2233E5", "dmc1_voltage", "byte / 10 -> volts"),),
)
DMC_1E = _module_profile(
    "dmc-1e", "1E", "drive motor controller 3 (DMC3)", _DMC_PROVENANCE,
    (("2233E5", "dmc3_voltage", "byte / 10 -> volts"),),
)

#: The battery identifiers, aimed at the *second* battery system manager.
#:
#: This vehicle named two: CB and CD.  Every enhanced read so far has gone to
#: CB.  Whether CD mirrors it, holds a different half of the pack, or refuses
#: outright is unknown and worth one supervised question each.
BSM_CD = _module_profile(
    "bsm-cd", "CD", "second battery system manager",
    "identifiers already proven on this vehicle at module CB; module CD is the "
    "second BSM this vehicle named for itself and has never been asked anything",
    (
        ("2227C6", "soc_cd", "[B4:B5]/655.35 percent"),
        ("2227AF", "energy_cd", "[B4:B5]/100"),
        ("222AF5", "cell_stats_cd", "three 16-bit / 10000 volts"),
        ("222B43", "array_cd", "raw, undecoded"),
    ),
)

#: Traction pack voltage and current, at drive motor controller 17.
#:
#: This is the gap the whole project has been pointing at.  Both identifiers
#: come from unmerged, single-author, BEV3 reports, which is weaker evidence
#: than anything else here -- but both are pure reads, and 0x2414 arrives with
#: test vectors its formula reproduces exactly.
#:
#: If both answer, they cross-check each other and the rest of the project:
#: volts * amps should equal the charge power already derived independently
#: from the energy field's slope, and the current should be NEGATIVE while
#: charging.  Two unmerged sources agreeing with a measurement taken a
#: different way would be worth far more than either on its own.
PACK_POWER = _module_profile(
    "pack-power", "17", "traction pack voltage and current (DMCM1)",
    "0x2885 from meatpiHQ/wican-fw issue #884 (2027 Bolt, BEV3, unmerged); "
    "0x2414 from OBDb/Cadillac-LYRIQ PR #14 (2025 Lyriq, BEV3, unmerged, "
    "ships test vectors). Neither is BT1; both unproven on this vehicle",
    (
        ("222885", "pack_voltage_candidate", "[B0:B1]/100 -> volts (payload-relative)"),
        ("222414", "pack_current_candidate", "signed16 / 20 -> amps, negative = charging"),
    ),
)

#: The battery manager, asked the five identifiers a BEV3 Bolt answers that
#: this vehicle has never been asked.  Module CB already answers eight others,
#: so the addressing is proven and only the identifiers are in question.
BSM_NEXT = EnhancedProfile(
    key="bsm-next",
    description="unproven BEV3 candidates against the battery manager (CB)",
    provenance=(
        "meatpiHQ/wican-fw issue #884, a 2027 Bolt on the same Ultium/BEV3 "
        "extended-addressing scheme this vehicle uses. UNMERGED single-author "
        "report, BEV3 not BT1 -- allowlisted to be tested, nothing here claims "
        "it works on this vehicle"
    ),
    init=BT1.init,
    requests=(
        ("2227BF", "regen_related_candidate", "charge-cycle regen field, scaling unknown"),
        ("2227BB", "thermal_energy_candidate", "thermal-management energy, scaling unknown"),
        ("2227B5", "thermal_distance_candidate", "thermal-management distance, scaling unknown"),
        ("222709", "ac_compressor_temp_candidate", "A/C compressor temperature, scaling unknown"),
        ("222AF1", "battery_module_temp_candidate", "battery module temperature, scaling unknown"),
    ),
    tx_id="0x14DACBF1",
    rx_id="0x142AF1CB",
)

#: The body control module.  This vehicle named 40 as BCM-BodyControl in its
#: own service 09 inventory and has never been asked anything.  The identifiers
#: come from the same LYRIQ pull request that supplied 2414, which this vehicle
#: does answer -- that is the whole reason to take its other register families
#: seriously rather than treating them as guesses.
BCM_40 = EnhancedProfile(
    key="bcm-40",
    description="unproven BEV3 candidates against the body control module (40)",
    provenance=(
        "OBDb/Cadillac-LYRIQ PR #14, which ships real-vehicle test vectors and "
        "is the same source that supplied 2414 (proven on this vehicle). "
        "UNMERGED, 2025 Lyriq BEV3 not BT1 -- allowlisted to be tested"
    ),
    init=(
        "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
        "ATSP7",
        "ATCP14",
        "ATSHDA40F1",      # target the BCM, tester F1
        "ATCRA142AF140",   # that module's reply
        "ATFCSH14DA40F1",
        "ATFCSD300000",
        "ATFCSM1",
        "ATST96",
    ),
    requests=(
        ("224149", "evse_pilot_current_candidate", "EVSE advertised current, scaling unknown"),
        ("22416C", "pack_group_voltage_1_candidate", "battery group voltage 1, scaling unknown"),
        ("22416D", "pack_group_voltage_2_candidate", "battery group voltage 2, scaling unknown"),
        ("22416E", "pack_group_voltage_3_candidate", "battery group voltage 3, scaling unknown"),
        ("22434F", "hv_battery_temp_candidate", "HV battery temperature, scaling unknown"),
        ("224127", "hv_battery_temp_a_candidate", "HV battery temperature A, scaling unknown"),
        ("224124", "hv_battery_temp_b_candidate", "HV battery temperature B, scaling unknown"),
        ("2240E5", "coolant_temp_1_candidate", "battery coolant temperature 1, scaling unknown"),
        ("2240E6", "coolant_temp_2_candidate", "battery coolant temperature 2, scaling unknown"),
    ),
    tx_id="0x14DA40F1",
    rx_id="0x142AF140",
)


#: ISO 14229-1 identification identifiers, used to ask a module whether it is
#: reachable at all rather than whether it holds a particular vendor value.
_ISO_REACH = (
    ("22F187", "iso_spare_part_number", "ISO 14229-1 vehicleManufacturerSparePartNumber"),
    ("22F188", "iso_ecu_software_number", "ISO 14229-1 vehicleManufacturerECUSoftwareNumber"),
    ("22F189", "iso_ecu_software_version", "ISO 14229-1 vehicleManufacturerECUSoftwareVersionNumber"),
    ("22F191", "iso_ecu_hardware_number", "ISO 14229-1 vehicleManufacturerECUHardwareNumber"),
)

_REACH_PROVENANCE = (
    "ISO 14229-1 standardised identification DataIdentifiers, not vendor "
    "identifiers and not guesses. Used to separate 'is this module reachable' "
    "from 'does this module have that identifier' -- the two questions nine "
    "NO DATA replies from module 40 could not tell apart on 2026-09-03"
)

#: Module 40 at the priority every working module on this vehicle uses.
BCM_40_REACH = _module_profile(
    "bcm-40-reach", "40", "is the body control module reachable at all",
    _REACH_PROVENANCE, _ISO_REACH,
)

#: The same four identifiers at module CD, which is a different case entirely.
#: CD answered 7F 22 31 to CB's identifiers, so it is known to be reachable and
#: to speak service 22 -- what is unknown is its own namespace.  A standard
#: identifier is the one request that should succeed regardless of namespace,
#: so a positive answer here would give the first real content from CD, and a
#: 7F 22 31 would say something quite odd about it.
BSM_CD_REACH = _module_profile(
    "bsm-cd-reach", "CD", "standard identification from the second battery manager",
    _REACH_PROVENANCE, _ISO_REACH,
)

#: The five identifiers proven at CB on 2026-09-03, aimed at its sibling.
#: Earlier CD probing used the older CB identifiers and drew 7F 22 31; these
#: five had not been discovered yet, so they have never been asked of CD.
BSM_CD_NEXT = _module_profile(
    "bsm-cd-next", "CD", "every CB-proven identifier CD has never been asked",
    "identifiers proven at module CB on this vehicle. The earlier CD probe put "
    "only four of them to it -- 27C6, 27AF, 2AF5, 2B43 -- and drew 7F 22 31 on "
    "all four. The nine below have never been asked of CD at all: four were "
    "proven at CB before that probe and simply were not included, and five were "
    "only discovered on 2026-09-03",
    (
        # Proven at CB well before the CD probe, and left out of it.
        ("2227C7", "range_cd", "[B4:B6]/103 miles at CB"),
        ("2227C0", "dist_since_charge_cd", "[B4:B6]/16.09344 miles at CB"),
        ("220046", "temperature_cd", "(B-40)*1.8+32 F at CB"),
        ("225401", "charger_field_cd", "raw; not power, see PACK_ARCHITECTURE"),
        # Discovered at CB on 2026-09-03.
        ("2227BF", "regen_candidate_cd", "unknown scaling"),
        ("2227BB", "thermal_energy_candidate_cd", "unknown scaling"),
        ("2227B5", "thermal_distance_candidate_cd", "unknown scaling"),
        ("222709", "compressor_temp_candidate_cd", "unknown scaling"),
        ("222AF1", "module_temp_array_candidate_cd", "24 values at CB; unknown here"),
    ),
)


#: Module 40, asked at priority 0x18 instead of 0x14.
#:
#: Thirteen identifiers drew NO DATA from 14DA40F1 and the conclusion recorded
#: was "the request is not arriving".  A per-module support census then had
#: module 40 answer service 01 and service 09 at *18*DA40F1, advertising PIDs
#: 01 and 42 and service 09 items 04, 06 and 0A.  So the request arrives
#: perfectly well; it was the priority that was wrong, and every earlier
#: conclusion about this module was drawn from thirteen requests sent to the
#: wrong place.
#:
#: The census also proves the receive filter isolates rather than returning one
#: loud responder: module 17 advertised nine service 01 PIDs where every other
#: module advertised two.
BCM_40_P18 = EnhancedProfile(
    key="bcm-40-p18",
    description="body control module, asked at the priority it actually answers",
    provenance=(
        "priority established by this project's own per-module support census "
        "on 2026-09-03: module 40 answers services 01 and 09 at 18DA40F1 after "
        "being silent to thirteen identifiers at 14DA40F1. Identifiers are the "
        "ISO 14229-1 standard identification set plus OBDb/Cadillac-LYRIQ PR #14"
    ),
    init=(
        "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
        "ATSP7",
        "ATCP18",          # the priority the census proved it answers at
        "ATSHDA40F1",
        "ATCRA18DAF140",   # and the reply address that goes with it
        "ATFCSH18DA40F1",
        "ATFCSD300000", "ATFCSM1", "ATST96",
    ),
    requests=(
        ("22F187", "iso_spare_part_number", "ISO 14229-1 standard"),
        ("22F191", "iso_ecu_hardware_number", "ISO 14229-1 standard"),
        ("224149", "evse_pilot_current_candidate", "LYRIQ PR #14"),
        ("22416C", "pack_group_voltage_1_candidate", "LYRIQ PR #14"),
        ("22434F", "hv_battery_temp_candidate", "LYRIQ PR #14"),
        ("2240E5", "coolant_temp_1_candidate", "LYRIQ PR #14"),
    ),
    tx_id="0x18DA40F1",
    rx_id="0x18DAF140",
)


def _p18_profile(key, ecu, description, provenance, requests):
    """A profile at priority 0x18 -- the one the legislated services use.

    ``_module_profile`` hardcodes ``ATCP14`` because that is what the battery
    manager needs, and every module probed through it inherited that choice.
    Module 40 was silent to thirteen identifiers as a result, and the silence
    was recorded as "the request is not arriving".  It was arriving; it was
    being sent at a priority that module does not answer.
    """
    return EnhancedProfile(
        key=key, description=description, provenance=provenance,
        init=(
            "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATAL",
            "ATSP7", "ATCP18",
            f"ATSHDA{ecu}F1",
            f"ATCRA18DAF1{ecu}",
            f"ATFCSH18DA{ecu}F1",
            "ATFCSD300000", "ATFCSM1", "ATST96",
        ),
        requests=requests,
        tx_id=f"0x18DA{ecu}F1",
        rx_id=f"0x18DAF1{ecu}",
    )


#: The rest of the LYRIQ set, at the priority module 40 actually answers.
BCM_40_FULL = _p18_profile(
    "bcm-40-full", "40", "every LYRIQ candidate at module 40, priority 0x18",
    "OBDb/Cadillac-LYRIQ PR #14. Four of these answered at priority 0x18 on "
    "2026-09-03 after all nine were silent at 0x14; this asks the remainder",
    (
        ("22416D", "pack_group_voltage_2_candidate", "LYRIQ PR #14"),
        ("22416E", "pack_group_voltage_3_candidate", "LYRIQ PR #14"),
        ("224127", "hv_battery_temp_a_candidate", "LYRIQ PR #14"),
        ("224124", "hv_battery_temp_b_candidate", "LYRIQ PR #14"),
        ("2240E6", "coolant_temp_2_candidate", "LYRIQ PR #14"),
        ("22416C", "pack_group_voltage_1_repeat", "repeat, to pair with 2 and 3"),
    ),
)

#: The same question asked of the second battery manager.  CD refused
#: seventeen identifiers at priority 0x14, and that was written up as "closed
#: from this access path".  Module 40 has just shown what a wrong priority
#: looks like, so the conclusion has to be retested rather than trusted.
BSM_CD_P18 = _p18_profile(
    "bsm-cd-p18", "CD", "second battery manager at priority 0x18",
    "module 40 answered at 0x18 after being silent at 0x14, so CD's refusals "
    "at 0x14 are retested here before the earlier conclusion is trusted",
    (
        ("22F187", "iso_spare_part_number", "ISO 14229-1 standard"),
        ("2227C6", "soc_cd", "proven at CB"),
        ("222AF5", "cell_stats_cd", "proven at CB"),
        ("222AF1", "module_temp_array_cd", "proven at CB"),
    ),
)


PROFILES: dict[str, EnhancedProfile] = {
    BT1.key: BT1,
    PACK_POWER.key: PACK_POWER,
    CHASSIS_BSCM.key: CHASSIS_BSCM,
    DMC_17.key: DMC_17,
    DMC_1E.key: DMC_1E,
    BSM_CD.key: BSM_CD,
    BEV3_BSM.key: BEV3_BSM,
    BEV3_DMCM.key: BEV3_DMCM,
    BSM_NEXT.key: BSM_NEXT,
    BCM_40.key: BCM_40,
    BCM_40_REACH.key: BCM_40_REACH,
    BSM_CD_REACH.key: BSM_CD_REACH,
    BSM_CD_NEXT.key: BSM_CD_NEXT,
    BCM_40_P18.key: BCM_40_P18,
    BCM_40_FULL.key: BCM_40_FULL,
    BSM_CD_P18.key: BSM_CD_P18,
}


@dataclass
class EnhancedResult:
    """What one supervised run observed.  Serialised straight to evidence."""

    profile: str
    started_utc: str
    confirmed: bool
    adapter_voltage: str = ""
    init_log: list[dict] = field(default_factory=list)
    reads: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)


def candidate_scalings(payload: bytes) -> list[dict]:
    """Every two-byte window of *payload*, under several candidate scalings.

    The point is to avoid committing to a published byte offset that has
    already moved once.  An operator reads the dashboard, looks down this
    table, and finds the window that matches -- which is evidence, where
    applying one formula and getting a number in range is only a coincidence
    that has not been ruled out.

    ``/655.35`` is the published BT1 scaling (a 16-bit full-scale percentage).
    ``/100`` and ``/2.55`` cover the other two conventions common in GM data,
    and the raw value is kept so nothing depends on this function being right.
    """
    windows: list[dict] = []
    for i in range(len(payload) - 1):
        raw = (payload[i] << 8) | payload[i + 1]
        windows.append(
            {
                "offset": f"B{i}:B{i + 1}",
                "hex": f"{payload[i]:02X}{payload[i + 1]:02X}",
                "raw": raw,
                "div_655_35": round(raw / 655.35, 3),
                "div_100": round(raw / 100.0, 3),
                "div_2_55": round(raw / 2.55, 3),
                "signed": raw - 0x10000 if raw & 0x8000 else raw,
            }
        )
    return windows


def candidate_triples(payload: bytes) -> list[dict]:
    """Every three-byte window of *payload*.

    Two of the published identifiers describe three-byte fields, so a
    two-byte-only table would silently have nothing to offer for them.
    """
    windows: list[dict] = []
    for i in range(len(payload) - 2):
        raw = (payload[i] << 16) | (payload[i + 1] << 8) | payload[i + 2]
        windows.append(
            {
                "offset": f"B{i}:B{i + 2}",
                "hex": payload[i : i + 3].hex().upper(),
                "raw": raw,
                "div_103": round(raw / 103.0, 3),
                "div_16_09344": round(raw / 16.09344, 3),
            }
        )
    return windows


def _describe_reply(reply: AdapterReply, request: str) -> dict:
    """Turn a parsed reply into an evidence record, decoding nothing blindly."""
    service = int(request[:2], 16)
    did = request[2:]
    record: dict = {
        "request": request,
        "did": did,
        "status": reply.status,
        "raw": reply.raw,
        "lines": reply.lines,
        "frames": [f.hex().upper() for f in reply.frames],
        "frame_headers": reply.frame_headers,
        "incomplete_frames": reply.incomplete,
        "negative_responses": [
            {
                "service": f"{svc:02X}",
                "code": f"{code:02X}",
                "name": negative_response_name(code),
            }
            for svc, code in reply.negative_responses
        ],
    }

    # A positive ReadDataByIdentifier response echoes the identifier: the reply
    # to 22 27 C6 starts 62 27 C6.  Requiring that echo is what stops an
    # unrelated frame that happened to survive the receive filter from being
    # read as an answer.
    expect = bytes([service + 0x40]) + bytes.fromhex(did)
    payload = None
    for frame in reply.frames:
        idx = frame.find(expect)
        if idx != -1:
            payload = frame[idx + len(expect):]
            record["positive_response"] = True
            record["echo_offset_in_frame"] = idx
            record["payload_hex"] = payload.hex().upper()
            record["payload_len"] = len(payload)
            break

    if payload is None:
        record["positive_response"] = False
    elif payload:
        # Scalings are computed over the *reassembled data bytes only*, so B0
        # here is the first byte after the echoed identifier.  The published
        # profile counts from the start of the response including the echo, so
        # its "B8:B9" is this table's "B5:B6"; both are printed rather than
        # silently reconciled, because which convention the profile used is
        # exactly the thing that is uncertain.
        record["scalings_from_data"] = candidate_scalings(payload)
        record["triples_from_data"] = candidate_triples(payload)
        # Single-byte readings too: TMP_A is (B4-40)*1.8+32 over one byte.
        record["bytes_from_data"] = [
            {"offset": f"B{i}", "hex": f"{b:02X}", "raw": b,
             "minus40_c": b - 40, "minus40_f": round((b - 40) * 1.8 + 32, 1)}
            for i, b in enumerate(payload)
        ]
        record["scalings_from_response"] = candidate_scalings(expect + payload)
        # The published profile counts B0 from the first byte of the whole CAN
        # frame -- identifier, then ISO-TP PCI, then the response -- so its
        # "B8:B9" only lines up when the four header bytes and the PCI byte are
        # included.  Computing that window here is what turns "the number looks
        # right" into a checkable claim about which bytes the profile meant.
        header_hex = ""
        if reply.frame_headers:
            for candidate, frame in zip(reply.frame_headers, reply.frames):
                if expect in frame:
                    header_hex = candidate
                    break
        for frame in reply.frames:
            idx = frame.find(expect)
            if idx == -1:
                continue
            whole = bytes.fromhex(header_hex) + frame if header_hex else frame
            record["can_frame_hex"] = whole.hex().upper()
            record["scalings_from_can_frame"] = candidate_scalings(whole)
            break
    return record


def run_profile(
    profile: EnhancedProfile,
    transport: Transport,
    *,
    say: Callable[[str], None] = lambda m: None,
    timeout: float = 8.0,
) -> EnhancedResult:
    """Send one profile's init sequence and its request(s), exactly once each."""
    result = EnhancedResult(
        profile=profile.key,
        started_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        confirmed=True,
    )

    for command in profile.init:
        safe = validate_command(command)
        try:
            response = transport.send(safe, timeout=timeout)
        except TransportError as exc:
            result.errors.append(f"{safe}: {exc}")
            say(f"  {safe}: TRANSPORT ERROR {exc}")
            return result
        reply = parse_reply(response.data)
        result.init_log.append(
            {"command": safe, "status": reply.status, "raw": reply.raw.strip()}
        )
        say(f"  {safe:<16} -> {reply.raw.strip()!r}")

    # Connector voltage is recorded alongside the read because it is the one
    # cheap way to tell, after the fact, whether the vehicle was awake when the
    # question was asked.  A NO DATA at 12.8 V and a NO DATA at 13.8 V are
    # different results and must not be filed as the same one.
    try:
        volts = parse_reply(transport.send("ATRV", timeout=timeout).data)
        # ``raw`` still carries the adapter's carriage returns and its ">"
        # prompt, which would go straight into the evidence file as control
        # characters.  ``lines`` is the same text already split and with the
        # prompt dropped, so it is the right source for a field that is read
        # as a value rather than as a transcript.
        result.adapter_voltage = " ".join(volts.lines)
        say(f"  ATRV             -> {result.adapter_voltage!r}")
    except TransportError as exc:  # pragma: no cover - transport failure path
        result.errors.append(f"ATRV: {exc}")

    for request, signal, published in profile.requests:
        safe = validate_enhanced_command(request)
        say(f"\n  sending {safe} ({signal}) ...")
        try:
            response = transport.send(safe, timeout=timeout)
        except TransportError as exc:
            result.errors.append(f"{safe}: {exc}")
            say(f"  {safe}: TRANSPORT ERROR {exc}")
            continue
        reply = parse_reply(response.data)
        record = _describe_reply(reply, safe)
        record["signal"] = signal
        record["published_decoder"] = published
        record["elapsed_s"] = round(response.elapsed_s, 3)
        result.reads.append(record)
        say(f"  raw: {reply.raw.strip()!r}")
    return result


def _dry_run(profile: EnhancedProfile, say: Callable[[str], None]) -> EnhancedResult:
    """Show exactly what would be transmitted, and validate all of it."""
    result = EnhancedResult(
        profile=profile.key,
        started_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        confirmed=False,
    )
    say("DRY RUN - nothing is transmitted and no serial device is opened.")
    say(f"profile   : {profile.key} ({profile.description})")
    say(f"provenance: {profile.provenance}")
    say(f"addressing: request {profile.tx_id}  response {profile.rx_id}")
    say("")
    say("adapter setup (validated against the ordinary read-only allowlist):")
    for command in profile.init:
        say(f"  {validate_command(command)}")
    say("")
    say("enhanced request(s) (validated against the supervised enhanced allowlist):")
    for request, signal, published in profile.requests:
        safe = validate_enhanced_command(request)
        say(f"  {safe}   {signal}   published decoder: {published}")
        result.reads.append(
            {"request": safe, "signal": signal, "published_decoder": published,
             "status": "not_sent_dry_run"}
        )
    say("")
    say("Re-run with --confirm to transmit.  The vehicle should be awake and")
    say("attended, and the dashboard state of charge noted at the same moment.")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hummer-obd-enhanced",
        description=(
            "Supervised enhanced (UDS service 22) read.  Dry run by default; "
            "transmits only with --confirm."
        ),
    )
    parser.add_argument("--profile", default="bt1", choices=sorted(PROFILES))
    parser.add_argument("--device", default="/dev/rfcomm0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", help="write the evidence JSON here")
    parser.add_argument(
        "--raw-log",
        default="logs/enhanced-raw.jsonl",
        help="byte-exact transcript of the exchange (append-only)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually transmit.  Without this nothing is sent.",
    )
    args = parser.parse_args(argv)

    profile = PROFILES[args.profile]

    def say(message: str) -> None:
        print(message, flush=True)

    say(f"enhanced read: {len(ENHANCED_READ_DIDS)} identifier(s) on the supervised allowlist")
    try:
        if not args.confirm:
            result = _dry_run(profile, say)
        else:
            say(f"opening {args.device} ...")
            # A byte-exact transcript is not optional for this path.  The whole
            # value of a supervised enhanced read is being able to re-decode it
            # later from what the truck actually said, rather than from what
            # this build believed at the time.
            with RawLog(
                args.raw_log,
                "enhanced-read",
                meta={"role": "enhanced_read", "profile": profile.key,
                      "provenance": profile.provenance},
            ) as rawlog:
                # The transport re-validates every command itself.  Handing it
                # the enhanced gate is what lets the one allowlisted identifier
                # through; it stays *narrower* than the default for every other
                # OBD service, so this is not a widening.
                with SerialTransport(
                    args.device,
                    rawlog,
                    baudrate=args.baud,
                    validator=validate_enhanced_command,
                ) as transport:
                    result = run_profile(
                        profile, transport, say=say, timeout=args.timeout
                    )
    except UnsafeCommandError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"transport failed: {exc}", file=sys.stderr)
        return 3

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(result.to_json())
        say(f"\nevidence written to {args.output}")

    for record in result.reads:
        if record.get("positive_response"):
            say(f"\nPOSITIVE RESPONSE for {record['request']}: "
                f"payload {record.get('payload_hex')}")
            say("  candidate windows (data bytes only):")
            for window in record.get("scalings_from_data", []):
                say(f"    {window['offset']:<8} {window['hex']}  "
                    f"/655.35={window['div_655_35']:<10} "
                    f"/100={window['div_100']:<10} /2.55={window['div_2_55']}")
        elif record.get("negative_responses"):
            for nr in record["negative_responses"]:
                say(f"\nNEGATIVE RESPONSE for {record['request']}: "
                    f"7F {nr['service']} {nr['code']} ({nr['name']})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
