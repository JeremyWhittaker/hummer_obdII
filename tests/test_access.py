"""The access matrix, and the drift it is built to make impossible.

`docs/CAPABILITIES.md` claimed pack voltage was unavailable while `pack_v` was
being written to every recorded row, and said so two hundred lines above its own
correction. `docs/PASSIVE_CAN_VALIDATION.md` said pack current was still not
obtained after `0x2414` had been proven. Both survived for a day, and both were
found by a person reading carefully rather than by anything failing.

A sentence cannot notice that it has become false. A table held against the
recorder's own column list can, and that is what these tests do:

* nothing on the "cannot reach" list may be a column the recorder writes;
* every column the recorder writes must be attributed to a source;
* the gate matrix must show the collector refusing what it is supposed to
  refuse, by *asking it*, not by reading a comment.
"""

import ast
import os
import re
import unittest

from hummer_obd import access, drive, safety
from hummer_obd.access import (
    GATE_PROBES,
    GATES,
    REASONS,
    UNREACHABLE,
    gate_matrix,
    signal_rows,
)
from hummer_obd.confidence import CONFIDENCE
from hummer_obd.safety import FORBIDDEN_SERVICES

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOC = os.path.join(_ROOT, access.DOC_PATH)


class TestNothingUnreachableIsQuietlyReachable(unittest.TestCase):
    """The specific failure this module exists to prevent."""

    def test_no_unreachable_entry_names_a_column_the_recorder_writes(self):
        for item in UNREACHABLE:
            if not item.absent_column:
                continue
            with self.subTest(name=item.name):
                self.assertNotIn(
                    item.absent_column, drive.COLUMNS,
                    f"'{item.name}' is listed as unreachable, but the recorder "
                    f"writes '{item.absent_column}' every cycle. This is the "
                    "exact shape of the docs/CAPABILITIES.md defect.",
                )

    def test_citing_something_we_do_have_forces_saying_so(self):
        """Not a ban on the mention -- a requirement to acknowledge it.

        The blunt version of this test forbids an unreachable entry from naming
        any identifier the vehicle answers. That is wrong: "we record 0x2AF1's
        twenty-four values and cannot say what they mean" is the honest and
        useful sentence, and banning it pushes authors toward vaguer prose
        rather than clearer. So the rule is to name what you do have.
        """
        for item in UNREACHABLE:
            proven = sorted(
                f"0x{did}" for did, evidence in CONFIDENCE.items()
                if evidence.level >= 1 and f"0x{did}" in item.detail
            )
            if not proven:
                continue
            with self.subTest(name=item.name):
                self.assertTrue(
                    item.despite.strip(),
                    f"'{item.name}' cites {proven}, which this vehicle answers, "
                    "without saying what we do have. Fill in `despite`.",
                )

    def test_every_reason_is_one_of_the_declared_kinds(self):
        # "We cannot" has several very different meanings and conflating them is
        # how a hardware ceiling gets mistaken for a missing source.
        for item in UNREACHABLE:
            with self.subTest(name=item.name):
                self.assertIn(item.reason, REASONS)

    def test_every_entry_says_what_would_change_it(self):
        # A limit with no stated escape route reads as permanent, and most of
        # these are not.
        for item in UNREACHABLE:
            with self.subTest(name=item.name):
                self.assertGreater(len(item.would_change_it.strip()), 20,
                                   "state concretely what would lift this")


class TestEveryColumnIsAccountedFor(unittest.TestCase):
    def test_the_matrix_covers_the_recorder_exactly(self):
        self.assertEqual([r["column"] for r in signal_rows()],
                         list(drive.COLUMNS))

    def test_no_column_is_unattributed(self):
        orphans = [r["column"] for r in signal_rows()
                   if r["where"] == "UNATTRIBUTED"]
        self.assertEqual(orphans, [],
                         f"columns with no known source: {orphans}")

    def test_every_enhanced_column_carries_a_priority(self):
        # There is no universal CAN priority on this vehicle -- module 28
        # answers only at 0x14 and module 40 only at 0x18 -- so a row without
        # one is a row a reader cannot reproduce.
        for row in signal_rows():
            if row["identifier"].startswith("0x"):
                with self.subTest(column=row["column"]):
                    self.assertIn(row["priority"], ("0x14", "0x18"))


class TestTheGateMatrixShowsTheRealPolicy(unittest.TestCase):
    """Asking the gates, not describing them."""

    def matrix(self):
        return {r["command"]: r["gates"] for r in gate_matrix()}

    def test_every_forbidden_service_is_refused_by_every_gate(self):
        matrix = self.matrix()
        for command, verdicts in matrix.items():
            service = command[:2].upper()
            if service not in FORBIDDEN_SERVICES:
                continue
            for gate, accepted in verdicts.items():
                with self.subTest(command=command, gate=gate):
                    self.assertFalse(
                        accepted,
                        f"gate '{gate}' accepts {command}, a forbidden service")

    def test_the_collector_cannot_reach_service_22(self):
        matrix = self.matrix()
        self.assertFalse(matrix["2227C6"]["collector"],
                         "the unattended gate must refuse service 22 even for a "
                         "proven identifier")
        self.assertTrue(matrix["2227C6"]["enhanced"])

    def test_the_collector_cannot_reach_any_monitor_command(self):
        matrix = self.matrix()
        for command in ("STCMM0", "STCMM1", "STMA", "ATMA"):
            with self.subTest(command=command):
                self.assertFalse(matrix[command]["collector"])
                self.assertFalse(matrix[command]["enhanced"])
                self.assertFalse(matrix[command]["recorder"])

    def test_nearness_to_a_working_identifier_proves_nothing(self):
        # The matrix's most instructive pair: one step either side of an
        # identifier that works. One is refused and the other accepted, and the
        # difference is whether a source names it -- not where it sits.
        matrix = self.matrix()
        self.assertFalse(matrix["2227C5"]["enhanced"])
        self.assertTrue(matrix["2227C7"]["enhanced"])

    def test_a_control_character_in_a_probe_cannot_break_the_table(self):
        """One probe *is* a carriage-return-separated batch, because that is a
        real injection the gate must refuse. Written raw it split its own table
        row in half and made the document fail its own idempotency check --
        the same defect class as an unescaped pipe in a provenance string,
        which this project also shipped once.
        """
        block = access.render_matrix()
        self.assertIn(r"010D\r04", block)
        for line in block.splitlines():
            with self.subTest(line=line[:40]):
                self.assertNotIn("\r", line)
                self.assertNotIn("\t", line)

    def test_every_table_in_the_document_is_well_formed(self):
        """Generated *and* hand-written. A malformed table renders as silent
        nonsense rather than an error, so nothing downstream complains and a
        reader simply sees the wrong thing.

        Checking only the generated half would leave the hand-authored tables --
        the failure shapes, the verification commands, the reasoning index --
        entirely unguarded, and those are the ones a human edits.
        """
        with open(_DOC, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        tables, current, in_fence = [], [], False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
            if in_fence:
                continue
            if line.startswith("|"):
                current.append(line)
            elif current:
                tables.append(current)
                current = []
        if current:
            tables.append(current)

        self.assertGreater(len(tables), 4,
                           "the table scanner found almost nothing")
        for table in tables:
            header = table[0]
            width = len(re.findall(r"(?<!\\)\|", header))
            with self.subTest(table=header[:50]):
                self.assertGreaterEqual(len(table), 3,
                                        "a table needs a header, a rule and a row")
                self.assertRegex(table[1], r"^\|[\s:-]*\|",
                                 "the second line must be the header rule")
                for row in table:
                    self.assertEqual(
                        len(re.findall(r"(?<!\\)\|", row)), width,
                        f"row disagrees with its header ({width} delimiters): "
                        f"{row[:70]}")

    def test_the_table_check_would_notice_a_broken_row(self):
        # A structural assertion that has never been shown to fire may be
        # checking nothing.
        broken = "| a | b |\n|---|---|\n| one | two | three |"
        widths = {len(re.findall(r"(?<!\\)\|", line))
                  for line in broken.splitlines()}
        self.assertGreater(len(widths), 1)

    def test_command_batching_is_refused_everywhere(self):
        matrix = self.matrix()
        for command in ("010D;04", "010D\r04"):
            with self.subTest(command=command):
                self.assertFalse(any(matrix[command].values()))

    def test_the_gate_list_is_the_whole_gate_list(self):
        """A matrix missing a gate is a matrix that cannot be trusted.

        The document says "five gates, not one", and that number has to come
        from somewhere checkable: every ``validate_*`` callable exported by
        safety.py must appear as a column.
        """
        exported = {name for name in dir(safety)
                    if name.startswith("validate_")
                    and callable(getattr(safety, name))}
        covered = {gate.__name__ for _n, gate, _w in GATES}
        # `validate_all` is a batch wrapper, not a sixth policy -- it maps
        # `validate_command` over a sequence. Excluding it silently would be a
        # hole, so it is excluded explicitly and then checked below.
        self.assertEqual(
            exported - covered - {"validate_all"}, set(),
            f"safety.py exports gates the matrix does not show: "
            f"{sorted(exported - covered - {'validate_all'})}")
        self.assertEqual(len(GATES), 5)

    def test_the_batch_wrapper_applies_the_collector_gate_and_nothing_softer(self):
        # It is the only validator not shown as a matrix column, so it gets its
        # own assertion rather than an exemption: a permissive validate_all
        # would be invisible in the matrix and reachable from the probe.
        for command in ("04", "2227C6", "STMA", "2E1234"):
            with self.subTest(command=command):
                with self.assertRaises(safety.UnsafeCommandError):
                    safety.validate_all([command])
        self.assertEqual(safety.validate_all(["010D", "ATRV"]), ["010D", "ATRV"])

    def test_the_probe_set_covers_every_forbidden_service(self):
        # A gate matrix that silently omits a service is a matrix that cannot be
        # trusted as an inventory.
        probed = {c[:2].upper() for c, _ in GATE_PROBES}
        missing = sorted(FORBIDDEN_SERVICES - probed)
        self.assertEqual(
            missing, [],
            f"forbidden services with no row in the matrix: {missing}")


class TestTheDocumentMatchesTheCode(unittest.TestCase):
    def test_the_generated_block_is_current(self):
        with open(_DOC, encoding="utf-8") as handle:
            document = handle.read()
        self.assertEqual(
            document, access.splice(document),
            "docs/ACCESS_MATRIX.md is stale. Regenerate it:\n"
            "    PYTHONPATH=src python3 -m hummer_obd.access",
        )

    def test_a_document_without_markers_is_refused(self):
        with self.assertRaises(ValueError):
            access.splice("no markers here")

    def test_splicing_is_idempotent(self):
        with open(_DOC, encoding="utf-8") as handle:
            document = handle.read()
        self.assertEqual(access.splice(document),
                         access.splice(access.splice(document)))


class TestTheHubDocumentsLinksResolve(unittest.TestCase):
    """This page's whole job is pointing at other pages.

    A hub document with a broken link is worse than no hub document: it sends a
    reader somewhere that does not exist and looks authoritative doing it. There
    was no link check anywhere in this repository before this one.
    """

    LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    @classmethod
    def setUpClass(cls):
        with open(_DOC, encoding="utf-8") as handle:
            cls.text = handle.read()

    @staticmethod
    def anchor(heading):
        """GitHub's slug: lowercase, punctuation dropped, spaces to hyphens."""
        slug = heading.strip().lower()
        slug = "".join(c for c in slug if c.isalnum() or c in " -_")
        return slug.replace(" ", "-")

    def headings(self, text):
        return {self.anchor(line.lstrip("#").strip())
                for line in text.splitlines() if line.startswith("#")}

    def test_every_relative_link_points_at_a_file_that_exists(self):
        targets = [t for t in self.LINK.findall(self.text)
                   if not t.startswith(("http://", "https://", "#"))]
        self.assertGreater(len(targets), 5, "the link pattern stopped matching")
        for target in targets:
            path = target.split("#", 1)[0]
            with self.subTest(target=target):
                self.assertTrue(
                    os.path.exists(os.path.join(os.path.dirname(_DOC), path)),
                    f"{path} does not exist")

    def test_every_anchor_resolves_to_a_real_heading(self):
        own = self.headings(self.text)
        checked = 0
        for target in self.LINK.findall(self.text):
            if target.startswith(("http://", "https://")):
                continue
            path, _, anchor = target.partition("#")
            if not anchor:
                continue
            if path:
                full = os.path.join(os.path.dirname(_DOC), path)
                if not os.path.exists(full):
                    continue  # covered by the test above
                with open(full, encoding="utf-8") as handle:
                    available = self.headings(handle.read())
            else:
                available = own
            checked += 1
            with self.subTest(target=target):
                self.assertIn(anchor, available,
                              f"no heading in {path or 'this page'} yields "
                              f"anchor '{anchor}'")
        self.assertGreater(checked, 1, "no anchor links were actually checked")


class TestEveryDocumentsLinksResolve(unittest.TestCase):
    """Widened from the hub page to all of `docs/` after two breaks in one edit.

    The hub-only version was written, and within the hour a correction added to
    VALIDATION.md used `../docs/X.md` from inside `docs/` -- twice. The check
    that would have caught it existed and was pointed at one file. A link
    checker scoped to the document you happen to be editing is a link checker
    scoped to the wrong document.
    """

    DOCS = os.path.join(_ROOT, "docs")
    LINK = re.compile(r'\[[^\]]+\]\(([^)\s]+)')

    def markdown_files(self):
        found = [os.path.join(self.DOCS, n) for n in sorted(os.listdir(self.DOCS))
                 if n.endswith(".md")]
        found.append(os.path.join(_ROOT, "README.md"))
        return found

    def test_every_relative_link_in_every_document_resolves(self):
        files = self.markdown_files()
        self.assertGreater(len(files), 15, "no documents found to check")
        checked = 0
        for path in files:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            base = os.path.dirname(path)
            for target in self.LINK.findall(text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                filepart = target.split("#", 1)[0]
                if not filepart:
                    continue
                checked += 1
                with self.subTest(doc=os.path.basename(path), target=target):
                    self.assertTrue(
                        os.path.exists(os.path.join(base, filepart)),
                        f"{os.path.basename(path)} links to {filepart}, "
                        "which does not exist")
        self.assertGreater(checked, 40,
                           f"only {checked} links checked; the pattern is "
                           "probably not matching")


class TestThePublishedCapabilityReportCountsRatherThanAsserts(unittest.TestCase):
    """A fixed string inside a *generated* report is the worst place for one.

    `capabilities.py` published "GM/Ultium identifiers are unproven on this VIN"
    in every capability report until 2026-09-04, by which point 31 of 35 had
    answered and nine were cross-validated. Nothing noticed, because a generated
    report looks like a measurement whether or not the sentence inside it is.
    """

    def test_the_counts_come_from_the_confidence_table(self):
        from hummer_obd import capabilities
        self.assertEqual(
            capabilities._enhanced_proven(),
            sum(1 for e in CONFIDENCE.values() if e.level >= 1))
        self.assertEqual(
            capabilities._enhanced_production(),
            sum(1 for e in CONFIDENCE.values()
                if e.level >= capabilities.PRODUCTION_MINIMUM))

    def test_the_report_no_longer_claims_the_identifiers_are_unproven(self):
        from hummer_obd import capabilities
        source = open(capabilities.__file__, encoding="utf-8").read()
        # Allowed in the comment explaining the fix, not in the emitted string.
        emitted = [line for line in source.splitlines()
                   if "unproven on this VIN" in line and not line.lstrip().startswith("#")]
        self.assertEqual(emitted, [],
                         "the capability report asserts identifiers are unproven")


class TestItNeverTouchesTheVehicle(unittest.TestCase):
    """Checked against the module's imports, not against its prose.

    The obvious version of this test greps the source for "SerialTransport" and
    friends, which is what `tests/test_decode_fields.py` does. It fails here for
    the wrong reason: this module *documents* which gate `SerialTransport`
    defaults to, and a substring search cannot tell an explanation from a
    dependency. Reading the import graph can, and it is the thing actually being
    asserted.
    """

    FORBIDDEN_IMPORTS = {"serial", "hummer_obd.transport", "hummer_obd.monitor",
                         "hummer_obd.collector", "hummer_obd.probe",
                         "hummer_obd.enhanced", "hummer_obd.voltage",
                         "hummer_obd.discover"}

    def imported_names(self, module):
        """Every module name this module imports, absolute where resolvable."""
        with open(module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative: ".safety" -> "hummer_obd.safety"
                    names.add(f"hummer_obd.{node.module}" if node.module
                              else "hummer_obd")
                    names.update(f"hummer_obd.{alias.name}"
                                 for alias in node.names)
                elif node.module:
                    names.add(node.module)
        return names

    def test_it_imports_nothing_that_can_reach_the_vehicle(self):
        imported = self.imported_names(access)
        reachable = sorted(imported & self.FORBIDDEN_IMPORTS)
        self.assertEqual(reachable, [],
                         f"access.py imports {reachable}, which can transmit")

    def test_the_check_would_actually_catch_a_transmitter(self):
        # A negative assertion that has never been shown to fire is a test that
        # might be checking nothing. Point it at a module that genuinely can
        # transmit and confirm it objects.
        from hummer_obd import collector
        self.assertTrue(self.imported_names(collector) & self.FORBIDDEN_IMPORTS,
                        "the import check no longer detects a transmitting module")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
