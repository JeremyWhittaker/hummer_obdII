"""The identifier registry must not drift from the gate that enforces it.

`docs/GM_ENHANCED_CANDIDATES.md` is the provenance record for everything this
project is allowed to transmit. It was hand-maintained and fell thirty-six
commits behind the code, missing the traction-pack identifiers that closed the
project's largest gap. That is the third hand-kept inventory here to drift,
after the README's column count and the drive unit's identifier list.

These tests exist so there is not a fourth.
"""

import re
import unittest
from pathlib import Path

from hummer_obd import registry
from hummer_obd.safety import ENHANCED_READ_DIDS

DOC = Path(__file__).resolve().parents[1] / "docs" / "GM_ENHANCED_CANDIDATES.md"


class TestTheDocumentMatchesTheGate(unittest.TestCase):
    def test_the_generated_block_is_current(self):
        document = DOC.read_text(encoding="utf-8")
        self.assertEqual(
            document, registry.splice(document),
            "docs/GM_ENHANCED_CANDIDATES.md is stale. Regenerate it:\n"
            "    PYTHONPATH=src python3 -m hummer_obd.registry",
        )

    def test_every_transmittable_identifier_is_documented(self):
        document = DOC.read_text(encoding="utf-8")
        missing = [d for d in ENHANCED_READ_DIDS if f"`0x{d}`" not in document]
        self.assertEqual(
            missing, [],
            f"identifiers the gate would transmit but the record does not "
            f"mention: {missing}",
        )

    def test_the_count_in_the_document_is_the_real_count(self):
        self.assertIn(
            f"**{len(ENHANCED_READ_DIDS)} identifiers**",
            DOC.read_text(encoding="utf-8"),
        )


class TestRendering(unittest.TestCase):
    def test_every_identifier_appears_with_its_provenance(self):
        rendered = registry.render_registry()
        for did, provenance in ENHANCED_READ_DIDS.items():
            with self.subTest(did=did):
                self.assertIn(f"`0x{did}`", rendered)
                # The first few words of the provenance should survive.
                head = " ".join(provenance.split())[:30]
                self.assertIn(head, rendered)

    def test_identifiers_are_sorted_so_diffs_stay_readable(self):
        rows = [
            line for line in registry.render_registry().splitlines()
            if line.startswith("| `0x")
        ]
        self.assertEqual(rows, sorted(rows))

    def test_a_pipe_in_a_provenance_string_cannot_break_the_table(self):
        # Markdown tables are column-delimited by pipes, so an unescaped one in
        # a source URL or note would silently split a row into wrong columns.
        original = dict(ENHANCED_READ_DIDS)
        try:
            ENHANCED_READ_DIDS["FFFF"] = "source | with | pipes"
            row = [
                line for line in registry.render_registry().splitlines()
                if "`0xFFFF`" in line
            ][0]
            # Escaped pipes are still pipe characters, so counting them all
            # proves nothing.  What matters is how many delimit columns, which
            # is the count of pipes *not* preceded by a backslash.
            delimiters = len(re.findall(r"(?<!\\)\|", row))
            self.assertEqual(
                delimiters, 3,
                f"an unescaped pipe split the row into extra columns: {row}",
            )
            self.assertIn(r"source \| with \| pipes", row)
        finally:
            ENHANCED_READ_DIDS.clear()
            ENHANCED_READ_DIDS.update(original)

    def test_splicing_replaces_rather_than_appends(self):
        document = f"before\n{registry.BEGIN_MARKER}\nold\n{registry.END_MARKER}\nafter"
        spliced = registry.splice(document)
        self.assertNotIn("old", spliced)
        self.assertTrue(spliced.startswith("before"))
        self.assertTrue(spliced.endswith("after"))
        self.assertEqual(spliced.count(registry.BEGIN_MARKER), 1)

    def test_splicing_is_idempotent(self):
        document = DOC.read_text(encoding="utf-8")
        self.assertEqual(registry.splice(document), registry.splice(registry.splice(document)))

    def test_a_document_without_markers_is_refused_not_appended_to(self):
        # Two registries in one document is worse than one stale registry: at
        # least a stale one is obviously a single answer.
        with self.assertRaises(ValueError):
            registry.splice("a document with no markers at all")

    def test_markers_in_the_wrong_order_are_refused(self):
        with self.assertRaises(ValueError):
            registry.splice(f"{registry.END_MARKER}\n{registry.BEGIN_MARKER}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
