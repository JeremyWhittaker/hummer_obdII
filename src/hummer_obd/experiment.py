"""What a human observed while the recorder was running.

Every number this project holds comes from the vehicle, and that is a problem
for the fields it cannot decode. `0x4149` is an EVSE-current candidate read
while parked and unplugged; `0x434F` is a temperature candidate read at one
temperature; the module-40 thermal fields cover 9.0 degrees Fahrenheit of a
23.4 degree corpus. Correlating them against other vehicle-reported values can
only ever show that two of the truck's own numbers move together.

**What breaks that circle is an outside measurement**, and outside measurements
are things a person reads and forgets: the charger said 7.4 kW, the dashboard
said 68%, it was 4 degrees out, the climate system was off. None of it is in the
CSV, and by the time the analysis runs nobody remembers.

So this records it, as a sidecar beside the session rather than inside it:

    evidence/sessions/drive-20260904T010357Z.csv
    evidence/experiments/drive-20260904T010357Z.json

The split is deliberate. The CSV is what the vehicle said and must stay exactly
that; the sidecar is what a person claims, and a reader is entitled to weigh the
two differently. Nothing here can alter a recorded row.

The fields are all things a person can actually read off a display or a
thermometer. There is no field for "what I think this identifier means", because
that is a conclusion rather than an observation and it belongs in
`hummer_obd.confidence` where a test can check it.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import MISSING, asdict, dataclass, fields
from typing import Optional

__all__ = [
    "Experiment",
    "LABEL_SOURCES",
    "mark",
    "load_marks",
    "DEFAULT_MARKS",
    "VEHICLE_STATES",
    "CHARGE_STATES",
    "sidecar_path",
    "load",
    "load_all",
    "unlabelled_sessions",
    "main",
]

DEFAULT_SESSION_DIR = "evidence/sessions"
DEFAULT_EXPERIMENT_DIR = "evidence/experiments"

#: What the vehicle was doing.  A closed set, because "parked" and "parked and
#: awake" are different states and free text lets them blur.
VEHICLE_STATES: tuple[str, ...] = (
    "asleep",
    "parked-awake",
    "driving",
    "driving-highway",
    "regen-heavy",
    "stationary-ready",
)

#: Where the label came from, and this is the field that decides whether a
#: sidecar is worth anything.
#:
#: The whole point of an outside observation is to break the circle of
#: correlating the vehicle's numbers against the vehicle's other numbers. A
#: label *derived from the session CSV* does not break it -- "charge_state was
#: charging because pack current was negative" adds no information the analysis
#: did not already have, and treating it as ground truth would launder an
#: inference into evidence.
#:
#: Both kinds are worth recording. Only one is worth correlating against, and
#: the reader has to be able to tell which they are holding.
LABEL_SOURCES: tuple[str, ...] = (
    "observed-at-vehicle",
    "inferred-from-telemetry",
)

#: What the charging system was doing.  `plugged-idle` matters: an EVSE current
#: field read while plugged in and *not* drawing is a different observation from
#: one read unplugged, and both are different from one read mid-charge.
CHARGE_STATES: tuple[str, ...] = (
    "unplugged",
    "plugged-idle",
    "charging-ac",
    "charging-dc",
    "charge-complete",
)


@dataclass
class Experiment:
    """One session's worth of things a person observed.

    Every field is optional except the identity of the session and the two
    states, because a partial record is worth far more than none — and demanding
    a full set is how a form stops being filled in.
    """

    session: str
    vehicle_state: str
    charge_state: str
    #: See :data:`LABEL_SOURCES`. Required, with no default, because a default
    #: would be answered by whichever value someone chose and the distinction
    #: would stop being made.
    label_source: str
    #: Ambient air temperature in Fahrenheit, from a thermometer that is not the
    #: vehicle.  The single most valuable field here: the thermal identifiers
    #: cannot be decoded without temperature variation that is measured
    #: independently of the field being decoded.
    ambient_f: Optional[float] = None
    #: State of charge as the dashboard showed it.  The vehicle reports its own
    #: via 0x27C6; recording the display too is what would catch the two
    #: disagreeing.
    dashboard_soc_pct: Optional[float] = None
    #: What the charging equipment's own display said.  Independent of anything
    #: the truck reports, which is exactly why it is useful.
    evse_amps: Optional[float] = None
    evse_kw: Optional[float] = None
    evse_kwh_delivered: Optional[float] = None
    #: Cabin climate: "off", "heat", "cool", "defrost". Free text on purpose --
    #: this one is genuinely open-ended and a closed set would be wrong.
    hvac: str = ""
    #: Anything else. The place for "left it on a hill", "raining hard".
    notes: str = ""
    #: Who observed it, so a later reader knows whether to ask.
    observer: str = ""

    def problems(self) -> list[str]:
        """Everything wrong with this record, as a list rather than a raise.

        A sidecar with one bad field should still contribute its good ones.
        """
        bad: list[str] = []
        if not self.session.strip():
            bad.append("session is empty")
        if self.vehicle_state not in VEHICLE_STATES:
            bad.append(f"vehicle_state {self.vehicle_state!r} is not one of "
                       f"{list(VEHICLE_STATES)}")
        if self.charge_state not in CHARGE_STATES:
            bad.append(f"charge_state {self.charge_state!r} is not one of "
                       f"{list(CHARGE_STATES)}")
        if self.label_source not in LABEL_SOURCES:
            bad.append(f"label_source {self.label_source!r} is not one of "
                       f"{list(LABEL_SOURCES)}")
        # An inferred label cannot carry an outside measurement, by definition.
        # Letting it would be the exact laundering this field exists to stop.
        if self.label_source == "inferred-from-telemetry":
            outside = [n for n in ("ambient_f", "dashboard_soc_pct", "evse_amps",
                                   "evse_kw", "evse_kwh_delivered")
                       if getattr(self, n) is not None]
            if outside:
                bad.append(f"label_source is inferred-from-telemetry but "
                           f"{outside} are outside measurements; a value nobody "
                           "read off a display is not an observation")
        if self.ambient_f is not None and not -60.0 <= self.ambient_f <= 140.0:
            bad.append(f"ambient_f {self.ambient_f} is outside anywhere a "
                       "vehicle is driven")
        if self.dashboard_soc_pct is not None and not 0.0 <= self.dashboard_soc_pct <= 100.0:
            bad.append(f"dashboard_soc_pct {self.dashboard_soc_pct} is not a percentage")
        for name in ("evse_amps", "evse_kw", "evse_kwh_delivered"):
            value = getattr(self, name)
            if value is not None and value < 0:
                bad.append(f"{name} is negative; record magnitude, and put the "
                           "direction in charge_state")
        # The one cross-field check worth making: claiming a charge rate while
        # saying nothing was plugged in is a transcription error, and it is the
        # kind that quietly poisons a correlation.
        drawing = any(getattr(self, n) for n in ("evse_amps", "evse_kw"))
        if drawing and self.charge_state in ("unplugged", "charge-complete"):
            bad.append(f"an EVSE rate is recorded but charge_state is "
                       f"{self.charge_state!r}")
        return bad

    @property
    def valid(self) -> bool:
        return not self.problems()


def sidecar_path(session_csv: str, experiment_dir: str = DEFAULT_EXPERIMENT_DIR) -> str:
    """Where the sidecar for *session_csv* lives."""
    stem = os.path.splitext(os.path.basename(session_csv))[0]
    return os.path.join(experiment_dir, f"{stem}.json")


def load(path: str) -> Experiment:
    """Read one sidecar. Unknown keys are an error, not a shrug.

    A typo in a field name would otherwise be silently dropped and the value
    lost, which for a hand-written record is the worst outcome: the observation
    was made, written down, and thrown away.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    known = {f.name for f in fields(Experiment)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{path}: unknown field(s) {unknown}; known fields are "
                         f"{sorted(known)}")
    # Derived from the dataclass rather than listed here: a field with no
    # default IS a required field, and a hand-kept second list is one more thing
    # to forget.  It already drifted once -- `label_source` was added and this
    # line was not, so a sidecar missing it loaded with a TypeError from three
    # frames down instead of a message naming the field.
    required = [f.name for f in fields(Experiment)
                if f.default is MISSING and f.default_factory is MISSING]
    missing = [n for n in required if n not in data]
    if missing:
        raise ValueError(f"{path}: missing required field(s) {missing}")
    return Experiment(**data)


def load_all(experiment_dir: str = DEFAULT_EXPERIMENT_DIR) -> dict:
    """Every sidecar, keyed by session stem. Bad ones are reported, not hidden."""
    found: dict = {}
    problems: dict = {}
    for path in sorted(glob.glob(os.path.join(experiment_dir, "*.json"))):
        try:
            experiment = load(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            problems[os.path.basename(path)] = str(exc)
            continue
        found[os.path.splitext(os.path.basename(path))[0]] = experiment
    return {"experiments": found, "problems": problems}


def unlabelled_sessions(session_dir: str = DEFAULT_SESSION_DIR,
                        experiment_dir: str = DEFAULT_EXPERIMENT_DIR) -> list[str]:
    """Sessions with no sidecar.

    Reported rather than enforced. Most sessions are ordinary driving and need
    no label; the ones that matter are the charge, the cold morning, the fast
    charge -- and those are exactly the ones somebody will mean to write up
    later and not.
    """
    labelled = set(load_all(experiment_dir)["experiments"])
    return [os.path.basename(p) for p in
            sorted(glob.glob(os.path.join(session_dir, "drive-*.csv")))
            if os.path.splitext(os.path.basename(p))[0] not in labelled]


DEFAULT_MARKS = "evidence/experiments/marks.jsonl"


def mark(label: str, *, path: str = DEFAULT_MARKS,
         when: Optional[str] = None) -> dict:
    """Record that something happened, right now, with a UTC timestamp.

    Deliberately time-keyed rather than session-keyed. An operator at the
    vehicle does not know which CSV the recorder is writing, may cross a session
    boundary mid-experiment, and should not have to care. The recorder stamps
    every row with `utc`; so does this; the analysis joins them.

    Append-only, one JSON object per line, for the same reason the raw log is:
    a mark written during an experiment must not be alterable by the analysis
    that reads it.
    """
    from datetime import datetime, timezone
    entry = {
        "utc": when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "label": " ".join(label.split()),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_marks(path: str = DEFAULT_MARKS) -> list[dict]:
    """Every mark, oldest first. A malformed line is reported, never skipped."""
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                out.append({"utc": "", "label": "", "corrupt": line[:80],
                            "lineno": lineno})
                continue
            out.append(entry)
    return sorted(out, key=lambda e: e.get("utc") or "")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record what a person observed while a session was running. "
                    "Offline: touches no vehicle and never alters a session CSV."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("record", help="write a sidecar for one session")
    add.add_argument("session", help="the session CSV this describes")
    add.add_argument("--vehicle-state", required=True, choices=VEHICLE_STATES)
    add.add_argument("--charge-state", required=True, choices=CHARGE_STATES)
    add.add_argument("--label-source", required=True, choices=LABEL_SOURCES,
                     help="observed-at-vehicle breaks the circle; "
                          "inferred-from-telemetry does not, and says so")
    add.add_argument("--ambient-f", type=float)
    add.add_argument("--dashboard-soc", type=float, dest="dashboard_soc_pct")
    add.add_argument("--evse-amps", type=float)
    add.add_argument("--evse-kw", type=float)
    add.add_argument("--evse-kwh", type=float, dest="evse_kwh_delivered")
    add.add_argument("--hvac", default="")
    add.add_argument("--notes", default="")
    add.add_argument("--observer", default="")
    add.add_argument("--dir", default=DEFAULT_EXPERIMENT_DIR)

    marker = sub.add_parser("mark", help="record that something just happened")
    marker.add_argument("label", nargs="+",
                        help='what happened, e.g. "hvac max cool on"')
    marker.add_argument("--at", dest="when",
                        help="UTC timestamp if not now, e.g. 2026-09-04T02:41:00Z")
    marker.add_argument("--file", default=DEFAULT_MARKS)

    marks = sub.add_parser("marks", help="list recorded marks")
    marks.add_argument("--file", default=DEFAULT_MARKS)

    check = sub.add_parser("check", help="validate sidecars and list unlabelled sessions")
    check.add_argument("--dir", default=DEFAULT_EXPERIMENT_DIR)
    check.add_argument("--sessions", default=DEFAULT_SESSION_DIR)

    args = parser.parse_args(argv)

    if args.command == "mark":
        entry = mark(" ".join(args.label), path=args.file, when=args.when)
        print(f"{entry['utc']}  {entry['label']}")
        return 0

    if args.command == "marks":
        entries = load_marks(args.file)
        if not entries:
            print(f"no marks in {args.file}")
            return 0
        for entry in entries:
            if entry.get("corrupt"):
                print(f"  line {entry['lineno']}: CORRUPT {entry['corrupt']}",
                      file=sys.stderr)
            else:
                print(f"  {entry['utc']}  {entry['label']}")
        return 0

    if args.command == "record":
        experiment = Experiment(
            session=os.path.basename(args.session),
            vehicle_state=args.vehicle_state,
            charge_state=args.charge_state,
            label_source=args.label_source,
            ambient_f=args.ambient_f,
            dashboard_soc_pct=args.dashboard_soc_pct,
            evse_amps=args.evse_amps,
            evse_kw=args.evse_kw,
            evse_kwh_delivered=args.evse_kwh_delivered,
            hvac=args.hvac, notes=args.notes, observer=args.observer,
        )
        problems = experiment.problems()
        if problems:
            for problem in problems:
                print(f"REFUSED: {problem}", file=sys.stderr)
            return 2
        path = sidecar_path(args.session, args.dir)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(asdict(experiment), handle, indent=2, sort_keys=True)
        print(f"wrote {path}")
        return 0

    loaded = load_all(args.dir)
    for name, problem in sorted(loaded["problems"].items()):
        print(f"BAD  {name}: {problem}", file=sys.stderr)
    invalid = {n: e.problems() for n, e in loaded["experiments"].items()
               if not e.valid}
    for name, problems in sorted(invalid.items()):
        for problem in problems:
            print(f"BAD  {name}: {problem}", file=sys.stderr)
    print(f"{len(loaded['experiments'])} sidecar(s), "
          f"{len(loaded['problems']) + len(invalid)} with problems")
    missing = unlabelled_sessions(args.sessions, args.dir)
    if missing:
        print(f"\n{len(missing)} session(s) with no observations recorded:")
        for name in missing[:12]:
            print(f"    {name}")
        if len(missing) > 12:
            print(f"    ... and {len(missing) - 12} more")
        print("\nMost need none. The ones that matter are the charge, the cold")
        print("morning and the fast charge -- the sessions somebody meant to")
        print("write up later and did not.")
    return 1 if (loaded["problems"] or invalid) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
