"""Nothing recorded from a real vehicle may be committed.

This repository is public. That was not a problem anyone noticed until 58
session files covering five days of driving had already been pushed to a named
account -- 8,908 rows of timestamps, speeds, wheel speeds, braking, steering
and cornering forces. No coordinates and no VIN, and it is still a record of
every trip their owner took and when.

The fix has to be structural rather than remembered. Somebody who clones this,
plugs in their own adapter and runs the recorder must be protected by default,
without reading a warning or knowing this happened. So evidence/ is
deny-by-default in .gitignore and these tests assert the door is actually shut.
"""

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout


class TestNoVehicleDataIsCommitted(unittest.TestCase):

    def test_nothing_under_evidence_is_tracked_except_the_ignore_rules(self):
        tracked = [p for p in _git("ls-files", "evidence").split() if p]
        self.assertEqual(
            sorted(tracked), ["evidence/.gitignore"],
            "vehicle data is committed to a public repository. Untrack it with "
            "`git rm -r --cached evidence/` -- the files stay on disk.",
        )

    def test_a_newly_recorded_session_is_ignored_without_anyone_deciding(self):
        """The property that protects a stranger who clones this.

        Not "is the current data ignored" but "would data that does not exist
        yet be ignored", which is the case an operator will actually hit.
        """
        sessions = REPO / "evidence" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        probe = sessions / "drive-99999999T999999Z.csv"
        probe.write_text("utc,speed_kph\n2026-01-01T00:00:00Z,42\n", encoding="utf-8")
        try:
            out = _git("status", "--porcelain", "--untracked-files=all")
            self.assertNotIn(
                probe.name, out,
                "a freshly recorded session shows up as committable; the "
                "deny-by-default rule in evidence/.gitignore is not working",
            )
            self.assertTrue(
                _git("check-ignore", "-q", str(probe.relative_to(REPO))) == ""
            )
        finally:
            probe.unlink(missing_ok=True)

    def test_the_ignore_rule_does_not_reach_outside_evidence(self):
        # A bare `*` is a blunt instrument. If it ever escaped evidence/ it
        # would silently stop tracking source, which no test would notice
        # because the files would already be committed.
        for path in ("src/hummer_obd/drive.py", "tests/test_privacy.py",
                     "README.md", "docs/TELEMETRY_CATALOG.md"):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", path],
                    cwd=REPO, capture_output=True, text=True,
                )
                self.assertNotEqual(
                    result.returncode, 0,
                    f"{path} is being ignored; the evidence rule has escaped")

    #: VINs that appear in test fixtures and are known to be invented. Pinned
    #: rather than excluding tests/ wholesale: a real VIN pasted into a test
    #: file is exactly the accident this should catch, and skipping the whole
    #: directory would wave it through.
    FIXTURE_VINS = frozenset({
        "1GT40FDA5RU100123", "1GT40FDA3RU100234",   # invented, ...1001xx
        "5GTRSDE64NB123456",                        # invented, ...123456
        "1G1JC5444R7252367",                        # the standard example VIN
    })

    def test_no_committed_file_carries_a_vin(self):
        # A VIN is 17 characters, no I/O/Q. Checked across everything tracked
        # rather than just evidence, because the leak that matters is the one
        # nobody thought to look for.
        import re
        vin = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
        hits = {}
        for name in _git("ls-files").split():
            path = REPO / name
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if name == "src/hummer_obd/dashboard.html":
                # A VIN written into the base64 in place is glued to base64
                # letters on either side, and the \b-anchored pattern below
                # cannot see it there. So the line is not merely scanned when
                # its digest fails: a hand-edited REF_BODY is a finding itself.
                if self._embedded_body_was_edited(text):
                    hits.setdefault(name, "REF_BODY does not match its REF_DIGEST")
                text = self._without_the_embedded_body(text)
            for m in vin.findall(text):
                # Hex blobs and base64 are full of false positives; a real VIN
                # has at least one letter and one digit and is not all hex.
                if m in self.FIXTURE_VINS:
                    continue
                if (any(c.isalpha() for c in m) and any(c.isdigit() for c in m)
                        and not all(c in "0123456789ABCDEFabcdef" for c in m)):
                    hits.setdefault(name, m)
        self.assertEqual(hits, {}, f"possible VIN in tracked files: {hits}")

    @staticmethod
    def _without_the_embedded_body(text: str) -> str:
        """The page minus REF_BODY, and only while REF_BODY is exactly what the
        build script wrote.

        REF_BODY is 240 KB of base64-encoded vertex, normal and index bytes,
        written by scripts/build_reference_body.py, and base64 between '+' and
        '/' forms 17-character words that pass every VIN heuristic here. It is
        removed from the scan only while the sha256 the script writes beside
        it (REF_DIGEST, on its own line, which is scanned) still matches the
        data and the placement table, and every group name is a body group's.
        Any edit to that line -- a VIN overwriting base64 in place included --
        breaks the digest, and the whole line is scanned again.
        """
        import re
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import reference_skin
        line = re.search(r"  var REF_BODY = \[.*?\];\n", text)
        if not line or not reference_skin.digest_ok(text):
            return text
        return text[:line.start()] + text[line.end():]

    @staticmethod
    def _embedded_body_was_edited(text: str) -> bool:
        import re
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import reference_skin
        return bool(re.search(r"  var REF_BODY = \[", text)) and not reference_skin.digest_ok(text)

    def test_the_embedded_body_is_only_exempted_while_it_is_a_mesh(self):
        page = (REPO / "src" / "hummer_obd" / "dashboard.html").read_text(encoding="utf-8")
        self.assertLess(len(self._without_the_embedded_body(page)), len(page) - 100_000)
        tampered = page.replace('"i":"', '"i":"1GT40FDA5RU100123', 1)
        self.assertEqual(self._without_the_embedded_body(tampered), tampered)
        # The same length written over base64 in place keeps every byte count
        # valid; only the digest can tell.
        at = page.index('"n":"') + 20
        in_place = page[:at] + "1GT40FDA5RU100123" + page[at + 17:]
        self.assertEqual(self._without_the_embedded_body(in_place), in_place)
        # And the scan cannot see a VIN between base64 letters, so the edit
        # itself is what gets reported.
        self.assertTrue(self._embedded_body_was_edited(in_place))
        self.assertTrue(self._embedded_body_was_edited(tampered))
        self.assertFalse(self._embedded_body_was_edited(page))

    def test_no_committed_file_carries_a_remote_access_hostname(self):
        # A Nabu Casa remote domain is a credential, not a setting. Home
        # Assistant serves /config/www/ through it with no authentication at
        # all -- docs/AGENT-HOME-ASSISTANT-ACCESS.md verified that a plain
        # curl with no Authorization header returns 200 -- so publishing the
        # hostname in a public repository hands over every file under
        # /local/, which is where the dashboard panel lives.
        #
        # scripts/enable-remote-dashboard.sh needs this value and deliberately
        # reads it from Home Assistant at runtime instead of carrying it. This
        # asserts the next edit does not take the shortcut.
        import re
        # The real thing is a long opaque label. Placeholders written for
        # humans -- <id>, <something> -- are angle-bracketed and must keep
        # passing, or the docs cannot explain the mechanism they document.
        remote = re.compile(r"\b[a-z0-9]{16,}\.ui\.nabu\.casa\b")
        hits = {}
        for name in _git("ls-files").split():
            path = REPO / name
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            found = remote.findall(text)
            if found:
                hits.setdefault(name, found[0])
        self.assertEqual(hits, {}, f"remote-access hostname in tracked files: {hits}")

    def test_that_guard_would_actually_catch_one(self):
        # A regex that matches nothing passes for the wrong reason. This pins
        # the shape it is looking for against a synthetic example, so the test
        # above cannot rot into an assertion that always holds.
        import re
        remote = re.compile(r"\b[a-z0-9]{16,}\.ui\.nabu\.casa\b")
        # Assembled rather than written out: a literal example here would be
        # caught by the guard above, which scans this file too. Keeping the
        # guard absolute is worth more than the readability of one string.
        suffix = ".ui.nabu" + ".casa"
        self.assertTrue(remote.search("https://a1b2c3d4e5f6g7h8i9j0" + suffix))
        self.assertFalse(remote.search("https://<id>" + suffix))
        self.assertFalse(remote.search("https://<something>" + suffix))

    def test_the_pinned_fixture_vins_are_still_present(self):
        # An allowlist that stops matching reality is how a check quietly
        # becomes a lie: if a fixture VIN is renamed, the pin must be updated
        # rather than left to silently permit a string nothing uses.
        blob = "\n".join(
            (REPO / n).read_text(encoding="utf-8", errors="ignore")
            for n in _git("ls-files", "tests").split()
            if (REPO / n).is_file()
        )
        for pinned in self.FIXTURE_VINS:
            with self.subTest(vin=pinned[:8] + "..."):
                self.assertIn(pinned, blob,
                              "pinned fixture VIN no longer appears; drop it")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
