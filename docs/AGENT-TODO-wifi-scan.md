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

## What was deliberately NOT done

No system policy was weakened on the dev workstation. Adding a polkit rule to grant
`wifi.scan` would silence the prompt but leave Pi-only code probing real
hardware on every dev machine, so the code fix is the correct one and was left
to whoever owns this repo.

Delete this file once the fix lands.
