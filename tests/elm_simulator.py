"""A small ELM327/STN adapter simulator on a PTY.

It exists so the real probe — the real transport, the real safety gate, the
real raw log — can be exercised end to end without a vehicle.  It answers a
handful of commands the way an OBDLink MX+ does, and it records everything it
was asked, so a test can assert that no forbidden command was ever written to
a real serial device.
"""

from __future__ import annotations

import os
import pty
import termios
import threading
import tty


class ElmSimulator:
    """Serves one PTY; ``device`` is the slave path to open with pyserial."""

    RESPONSES = {
        "ATZ": "ELM327 v1.5",
        "ATE0": "OK",
        "ATL0": "OK",
        "ATS0": "OK",
        "ATH1": "OK",
        "ATAT1": "OK",
        "ATI": "OBDLink MX+ r5.7",
        "AT@1": "OBDLink MX+",
        "AT@2": "STN2255 SN 123456",
        "STI": "STN2255 v5.7.0",
        "STDI": "OBDLink MX+ r5.7",
        "ATRV": "13.9V",
        "ATSP0": "OK",
        "ATDP": "ISO 15765-4 (CAN 11/500)",
        "ATDPN": "A6",
        "0100": "7E8 06 41 00 BE 3F A8 13",
        "0120": "7E8 06 41 20 90 07 E0 11",
        "0140": "7E8 06 41 40 FA DC A0 01",
        "0160": "NO DATA",
        "0105": "7E8 03 41 05 5A",
        "010C": "7E8 04 41 0C 1A F8",
        "010D": "7E8 03 41 0D 00",
        "0111": "7E8 03 41 11 2E",
        "011F": "7E8 04 41 1F 00 64",
        "012F": "7E8 03 41 2F 80",
        "0142": "7E8 04 41 42 33 A0",
        "0146": "7E8 03 41 46 4A",
        "015B": "7E8 03 41 5B C8",
        "015C": "NO DATA",
        "03": "7E8 02 43 00",
        "07": "7E8 02 47 00",
        "0A": "7E8 02 4A 00",
        "0902": ("7E8 10 14 49 02 01 31 47 31\r"
                 "7E8 21 4A 43 35 34 34 34 52\r"
                 "7E8 22 37 32 35 32 33 36 37"),
        "0904": "7E8 10 13 49 04 01 41 42 43\r7E8 21 44 45 46 47 48 49 4A",
        "090A": "NO DATA",
    }

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = dict(self.RESPONSES)
        if responses:
            self.responses.update(responses)
        self.master, self.slave = pty.openpty()
        # A pty echoes by default, so the simulator would read back its own
        # replies and answer them forever.  Raw mode on both ends stops that
        # and keeps the byte stream exactly as written.
        tty.setraw(self.master)
        tty.setraw(self.slave)
        termios.tcflush(self.master, termios.TCIOFLUSH)
        self.device = os.ttyname(self.slave)
        self.received: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "ElmSimulator":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            os.close(self.master)
        except OSError:
            pass
        try:
            os.close(self.slave)
        except OSError:
            pass

    def _serve(self) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                data = os.read(self.master, 128)
            except OSError:
                return
            if not data:
                return
            buffer += data
            while b"\r" in buffer:
                line, buffer = buffer.split(b"\r", 1)
                command = line.decode("ascii", "replace").strip().upper()
                if not command:
                    continue
                self.received.append(command)
                body = self.responses.get(command, "NO DATA")
                try:
                    os.write(self.master, (body + "\r\r>").encode("ascii"))
                except OSError:
                    return

    def __enter__(self) -> "ElmSimulator":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
