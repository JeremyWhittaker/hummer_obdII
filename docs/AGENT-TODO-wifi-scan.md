# TODO: Pi-only display code shells out to `nmcli` on the dev host

**Status:** open
**Filed:** 2026-09-02, from the workstation side (not from inside this repo's work)
**Severity:** low risk to the vehicle node, high nuisance to Jeremy

## Symptom

Jeremy gets a repeated GNOME password prompt on his RDP desktop reading
**"System policy prevents Wi-Fi scans"**. 36 prompts in 24 hours, in bursts
that track agent activity in this repo rather than any steady daemon.

`polkitd` names the caller verbatim:

```
polkitd: Operator of unix-session:7 FAILED to authenticate to gain authorization
for action org.freedesktop.NetworkManager.wifi.scan
for unix-process:1036311 [nmcli -t -f ACTIVE,SSID,SIGNAL device wifi]
```

## Cause

`src/hummer_obd/display/status.py::_read_wifi()` shells out to the real
`nmcli` on whatever machine it happens to run on:

```python
out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi"])
```

That is correct on the Pi. It is wrong on the dev workstation, which has a real
Wi-Fi card, so the call triggers a genuine Wi-Fi scan. Because Jeremy
works over GNOME Remote Desktop, his session is seatless (`Seat=`,
`Remote=yes`), so polkit cannot apply the `allow_active`/`allow_inactive`
branches of `org.freedesktop.NetworkManager.wifi.scan` and falls through to
`allow_any = auth_admin` — an admin password prompt, every time.

The `_run()` guard does not catch this:

```python
def _run(cmd, timeout=4.0):
    if not shutil.which(cmd[0]):
        return ""
```

`shutil.which` is the wrong test for "am I on the target hardware". It happens
to protect the neighbouring `vcgencmd measure_temp` call only because
`vcgencmd` is absent off-Pi. `nmcli` is present on virtually every Linux box,
so the Wi-Fi path executes for real.

Nothing installed this as a service — there is no systemd unit, user unit,
timer, or cron entry for it on the dev workstation. It is executed ad hoc whenever an agent
runs the status renderer or its tests outside the Pi.

## Requested fix

1. Gate `_read_wifi()` (and ideally the whole hardware-probe block in
   `status.py`) on an explicit "am I the Pi" check rather than on
   `shutil.which`. Reading `/proc/device-tree/model` for `Raspberry Pi`, or an
   explicit env/config flag set by the deploy, are both better tests than
   probing for a binary that exists everywhere.
2. Mock `_run` in the display tests. Nothing in `tests/` currently patches
   `_run` or `_read_wifi`, so running the display tests off-Pi shells out to
   real hardware commands. That is the same class of problem as the mutation
   testing trap already recorded in HANDOFF.md: test activity reaching out and
   touching something real.
3. No change is needed on the Pi itself, and none is needed to the vehicle
   safety gates. This does not touch `validate_command` or any OBD path.

## Stopgap applied on the workstation — the code fix is still wanted

On 2026-09-03 a polkit rule was added on the dev workstation
(`/etc/polkit-1/rules.d/45-nm-wifi-scan.rules`) granting
`org.freedesktop.NetworkManager.wifi.scan` to the `sudo` group, because the
prompts were interrupting Jeremy every few minutes. That silences the symptom
on **one machine only**. It does not fix anything in this repository:

- Pi-only code still probes real hardware whenever it runs off-Pi.
- Any other machine this repo is checked out on will prompt exactly the same
  way, with no rule in place.
- The unmocked `_run` in the display tests is unchanged.

So the fix above is still the real one. Once it lands, that rule can be removed
if carrying the exemption is unwanted.

Nothing in the vehicle safety path was touched: no change to
`validate_command`, `ALLOWED_OBD_MODES`, or any OBD gate.

Delete this file once the fix lands.
