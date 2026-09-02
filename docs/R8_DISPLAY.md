# Uniden R8 display status

The e-paper status page can consume the sanitized state published by the
separate `unidenr8-collector` process at:

```text
/home/jeremy/unidenr8/.state/state.json
```

The display does not connect to Bluetooth, open `/dev/rfcomm0`, or send an OBD
command. It reads one bounded JSON file during its existing 300-second status
cycle. A current R8 line temporarily occupies the Tailscale line; the sixth
`obd` line is unchanged. When the state file is missing, malformed, from an
unknown schema, or implausibly future-dated, the original Tailscale line is
used instead. A valid document older than 90 seconds is rendered only as
`r8 stale`.

The reader treats the state file as untrusted input. It requires schema 1,
checks the timestamp and field types, and constructs its own short line from
allowlisted bands, directions, link state, voltage, and GPS-lock status. It
never renders the collector's `display_line`, free-form notes or reasons,
device identifiers, positions, or raw packets.

This is a node-health status line, not a real-time radar-alert display. The
panel intentionally refreshes no more often than every five minutes to limit
wear. Active-alert field meanings also remain unconfirmed on this particular
R8 until a real detection is captured; the only hardware alert packet observed
so far was all-clear.

Deploying this repository copies the reader but does not restart the installed
display unit. After the R8 collector has passed its bounded coexistence trial,
restart only the display service to load this code:

```bash
sudo systemctl restart hummer-display.service
```

Do not restart, edit, or rebind `hummer-rfcomm.service` as part of this change.
