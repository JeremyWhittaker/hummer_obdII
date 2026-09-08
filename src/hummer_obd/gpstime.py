"""Set the system clock from the satellites, once, at boot.

This Pi has no real-time clock.  ``timedatectl`` reports ``RTC time: n/a``, so
on boot it restores whatever was saved at shutdown and waits for NTP -- which
needs a network the vehicle does not have when it is parked away from WiFi or
being driven.  That is not hypothetical: on 2026-09-08 the node came back from
a three-day outage with a restored clock, opened a session named for a time
three days earlier, and wrote a row asserting an odometer reading 107 km ahead
of where the vehicle had been at that timestamp.

The GPS fixes this properly.  Its time comes from the satellites, so it needs
no network and no battery, and it is available before the recorder starts.

Two things this deliberately does NOT do.  It does not run continuously -- one
step at boot is what the recorder needs, and a daemon disciplining the clock is
chrony's job, not this project's.  And it does not trust the receiver blindly:
SiRF chipsets, which is what this vehicle carries, are the family known for GPS
week-rollover bugs that report a date roughly 19.7 years early.  A clock "fixed"
to 2006 would be worse than the restored one it replaced, so the time is
range-checked before it is used.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import gps as gps_module

#: No satellite time before this is believed.  Its only job is catching a
#: rollover: a receiver reporting 2006 is broken, not early.  A fixed floor
#: rather than "before the build" so it cannot drift into meaninglessness.
EPOCH_FLOOR = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Nor after this.  Twenty years of headroom is far more than any rollover
#: offset and far less than the 2038-style dates a confused receiver emits.
EPOCH_CEILING = datetime(2046, 1, 1, tzinfo=timezone.utc)

#: Below this the clock is close enough and is left alone.  Stepping it for
#: milliseconds would churn every timestamp for no gain.
MIN_STEP_S: float = 2.0

#: How long to wait for a usable fix.  A cold receiver can take minutes, but
#: the recorder should not be held at boot indefinitely -- it is better to
#: record with a wrong clock, loudly, than not to record.
DEFAULT_WAIT_S: float = 90.0


def parse_gps_time(text: object) -> Optional[datetime]:
    """gpsd's ISO timestamp as an aware datetime, or ``None`` if unusable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def is_plausible(stamp: Optional[datetime]) -> bool:
    """Whether a satellite time is worth believing.

    The check that earns its place is the lower bound.  A SiRF receiver with a
    week-rollover fault reports a confident, well-formed, precise time about
    19.7 years early, and every other signal -- fix mode, satellite count --
    looks healthy while it does it.
    """
    return stamp is not None and EPOCH_FLOOR <= stamp < EPOCH_CEILING


def decide(
    gps_stamp: Optional[datetime],
    system_now: datetime,
    *,
    min_step_s: float = MIN_STEP_S,
) -> tuple[bool, str]:
    """Whether to step the clock, and the sentence explaining it either way."""
    if gps_stamp is None:
        return False, "no satellite time available"
    if not is_plausible(gps_stamp):
        return False, (
            f"satellite time {gps_stamp.isoformat()} is outside "
            f"{EPOCH_FLOOR.date()}..{EPOCH_CEILING.date()}; refusing it "
            "(a SiRF week-rollover fault looks exactly like this)")
    drift = (gps_stamp - system_now).total_seconds()
    if abs(drift) < min_step_s:
        return False, f"clock is within {abs(drift):.3f}s of the satellites"
    return True, (f"clock is {drift:+.1f}s from the satellites "
                  f"({system_now.isoformat()} -> {gps_stamp.isoformat()})")


def set_system_clock(stamp: datetime) -> None:  # pragma: no cover - needs root
    """Step the clock.  Raises ``PermissionError`` if not privileged."""
    import ctypes

    class _Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    epoch = stamp.timestamp()
    ts = _Timespec(int(epoch), int((epoch - int(epoch)) * 1e9))
    # CLOCK_REALTIME is 0.
    if libc.clock_settime(0, ctypes.byref(ts)) != 0:
        err = ctypes.get_errno()
        raise PermissionError(f"clock_settime failed (errno {err})")


def wait_for_time(
    reader: gps_module.GpsReader,
    *,
    wait_s: float = DEFAULT_WAIT_S,
    poll_s: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[datetime]:
    """Poll the reader until it offers a plausible satellite time, or give up."""
    deadline = clock() + wait_s
    while True:
        fix = reader.fix()
        if fix is not None:
            stamp = parse_gps_time(fix.get("time"))
            if is_plausible(stamp):
                return stamp
        if clock() >= deadline:
            return None
        sleeper(poll_s)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hummer-obd-gpstime",
        description=(
            "Set the system clock from the GPS. Reports what it would do and "
            "changes nothing unless --set is given."
        ),
    )
    parser.add_argument("--host", default=gps_module.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=gps_module.DEFAULT_PORT)
    parser.add_argument("--wait-s", type=float, default=DEFAULT_WAIT_S)
    parser.add_argument(
        "--set", action="store_true",
        help="actually step the clock (needs root); otherwise dry-run")
    args = parser.parse_args(argv)

    reader = gps_module.GpsReader(args.host, args.port).start()
    try:
        stamp = wait_for_time(reader, wait_s=args.wait_s)
    finally:
        reader.stop()

    if stamp is None:
        print(f"no usable satellite time within {args.wait_s:.0f}s: "
              f"{reader.describe()}", file=sys.stderr)
        # Not an error the caller should act on: a cold receiver in a garage
        # is normal, and the recorder must still start.
        return 0

    should, why = decide(stamp, datetime.now(timezone.utc))
    print(why)
    if not should:
        return 0
    if not args.set:
        print("dry run; re-run with --set to apply")
        return 0
    try:
        set_system_clock(stamp)
    except PermissionError as exc:
        print(f"cannot set the clock: {exc}", file=sys.stderr)
        return 1
    print(f"clock set to {stamp.isoformat()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
