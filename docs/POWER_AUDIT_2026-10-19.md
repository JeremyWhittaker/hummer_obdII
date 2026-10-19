# Power and boot audit — 2026-10-19

Local Claude transcript and live read-only inspection were compared with the
deployed source. Private transcript, network and hardware identifiers remain
outside Git.

| Item | Status | Evidence / remaining work |
|---|---|---|
| Claude PiSugar3 support | implemented | Commit `401a151`; live `hummer-battery` logs identify PiSugar3 and report plausible voltages |
| Claude service restart handoff | implemented | Current boot loaded the updated reader and dashboard: latest-session API has 29 rows, with `array_2b43` present in eight |
| UPS identity and restore configuration | implemented | PiSugar3 at `0x57`, firmware `v1.3.4`, 3.866 V, firmware estimate 84%, control `0x7C`: restore enabled, external input absent at measurement |
| Boot/service/storage health | implemented | No failed units; recorder, battery, API and display active; RFCOMM connected; no recorder/battery/display restarts; no recorded throttling; root filesystem 22% used, 22 GiB free; clock synchronized |
| Recorder awake-to-asleep behavior | implemented | Current-boot CSV has 29 rows; recorder logged modules ceasing replies, ended the session, and entered sleep backoff while adapter still answered |
| API boot recovery | implemented | One transient bind failure before the VPN address existed; service restarted once and serves the latest session with correct stale status |
| Graceful 10% reserve policy | implemented | `pisugar3_power.py` plus focused tests: board-reported percentage, three low readings 30 s apart, external-input guard, 3.40 V fallback, final fresh sample and checked systemctl result |
| Final UPS output cut | implemented | Tested late hook checks shutdown phase and read-only root, sets restore and zero cutoff delay, preserves unrelated bits, skips reboot, and handles returning input during automatic shutdown |
| Privileged installation | implemented | Operator ran the [installer](RUNBOOK.md#pisugar3-reserve-shutdown-installation); activation verified at 10:17 MST. Monitor runs as root with reserve 10%, 3.40 V floor, three readings 30 s apart, dry-run off; both root-owned program copies and service drop-in hashes match committed source. Recorder process unchanged |
| Automatic restart on vehicle start | deferred | Restore bit and installed late hook verified. Owner explicitly requested keeping the Pi online and testing later. Actual output cutoff followed by restored vehicle input and automatic boot remain unverified; verify switched feed goes to UPS input |
| GPS | implemented | Operator installed `install-gps-hotplug.sh`; verified at 14:12 MST. `/dev/hummer_gps` resolves to the current `ttyUSB0`; gpsd registers that alias with the SiRF driver and a live 3D fix. The recorder's reader also receives a 3D fix using eight satellites. Targeted udev rule and stock gpsdctl unit active; both recorders unchanged during verification. Physical unplug/replug acceptance remains pending |
| Historical system logs | blocked | Only current boot retained; journald logs are in `/run`, so earlier shutdowns cannot be audited. Application CSV/raw logs persist separately |
| E-paper/R8 | implemented | E-paper updates regularly; radar detector remains in connecting state. Owner confirmed the truck and detector are off, which explains the current disconnected state |
| Passive CAN to live model | deferred | Prior documented passive test returned zero bytes; continuous passive monitoring is not enabled. Existing supervised-only gate remains in place |
| Automatic parked scans | deferred | Earlier request remains a future feature; current discovery is person-started with parked/DTC guards, not triggered unattended |
| Continuous legacy collector | deferred | Still disabled until physical active-polling/sleep acceptance; the separate drive recorder is active |
| Signal labeling and 3D precision | deferred | Raw scan arrays still have unproven scaling/physical positions. No new semantic labels inferred in this audit |

The UPS percentage is a firmware estimate, not a capacity calibration. No
power-management registers, services, GPS configuration or vehicle diagnostic
settings were changed by the agent during the read-only health inspection.
The operator subsequently performed the manual root installation. Fresh
runtime checks confirmed the new reserve monitor is active, the restore bit
is set, and no services failed. The controlled physical shutdown test was
deferred at the owner's request. GPS was subsequently repaired through its
separate manual installer and verified against live receiver data.

An online Pi does not prove that the Hummer is currently supplying power. The
UPS explicitly reported no external input. Likewise `active` gpsd does not
prove a working receiver, and an available API correctly serves stale data
when the vehicle is asleep.

## GPS root repair

The receiver had re-enumerated as `/dev/ttyUSB1`, while `/etc/default/gpsd`
still contained `DEVICES="/dev/ttyUSB0"`. The operator installed the repair on
2026-10-19. Defaults now use `/dev/hummer_gps`, and the matched udev rule
provides automatic recognition and stock gpsdctl add/remove handling. At
14:12 MST, the alias resolved to `ttyUSB0`, gpsd served a live SiRF 3D fix,
and both recorders remained running without restarts. No failed systemd units
were present. The installed rule matches the committed generator exactly;
gpsd service and socket are enabled. Physical unplug/replug acceptance is
still pending.

For future installation or updates, run manually **on the Pi**:

```bash
sudo bash /home/jeremy/hummer-obd/scripts/install-gps-hotplug.sh
```

See [USB GPS naming and hotplug](RUNBOOK.md#usb-gps-naming-and-hotplug) for
registration checks, physical acceptance and rollback. An
active daemon with no satellite fix is different from a daemon serving no
device. This repair does not require stopping the drive recorder: its GPS
reader reconnects to the existing gpsd port.
