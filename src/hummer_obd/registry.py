"""Render the enhanced-identifier registry from the gate that enforces it.

`docs/GM_ENHANCED_CANDIDATES.md` is the project's provenance record: what may be
transmitted, where each identifier came from, and what happened when it was
tried. It was hand-maintained, and by 2026-09-03 it had fallen thirty-six
commits behind the code -- missing the traction-pack identifiers that closed the
project's largest gap, and every candidate added that day.

That is the third hand-kept inventory in this project to drift from the code,
after the README's column count and the drive unit's identifier list. The
pattern is clear enough to stop repeating: the list is now rendered from
:data:`hummer_obd.safety.ENHANCED_READ_DIDS`, which is the same table the safety
gate consults before anything reaches the wire. A test compares the rendered
text against the document, so the two cannot disagree without the suite going
red.

Only the table is generated. The reasoning around it -- why enhanced reads are
allowed at all, how a negative response is read, what each result meant -- stays
hand-written, because that is the part worth writing by hand.
"""

from __future__ import annotations

from .safety import ENHANCED_READ_DIDS

__all__ = ["BEGIN_MARKER", "END_MARKER", "render_registry", "splice"]

BEGIN_MARKER = "<!-- BEGIN GENERATED IDENTIFIER REGISTRY -->"
END_MARKER = "<!-- END GENERATED IDENTIFIER REGISTRY -->"


def _first_sentence(text: str) -> str:
    """The provenance line, collapsed to one table cell."""
    collapsed = " ".join(text.split())
    collapsed = collapsed.replace("|", "\\|")
    return collapsed


def render_registry() -> str:
    """The identifier table, exactly as the gate holds it."""
    lines = [
        BEGIN_MARKER,
        "",
        "<!-- Generated from hummer_obd.safety.ENHANCED_READ_DIDS by",
        "     hummer_obd.registry.render_registry(). Do not edit by hand:",
        "     tests/test_registry.py fails when this drifts from the code. -->",
        "",
        f"The safety gate currently admits **{len(ENHANCED_READ_DIDS)} identifiers**.",
        "Being listed here means *may be transmitted*, not *is known to work on",
        "this vehicle*. Results live in the tiers below and in",
        "[Probe, 2026-09-03](PROBE_2026-09-03.md).",
        "",
        "| Identifier | Provenance |",
        "|---|---|",
    ]
    for did in sorted(ENHANCED_READ_DIDS):
        lines.append(f"| `0x{did}` | {_first_sentence(ENHANCED_READ_DIDS[did])} |")
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def splice(document: str) -> str:
    """*document* with its generated block replaced by a freshly rendered one.

    Raises if the markers are missing, rather than appending a second copy: a
    document with two registries in it is worse than one with a stale registry,
    because at least a stale one is obviously a single answer.
    """
    start = document.find(BEGIN_MARKER)
    end = document.find(END_MARKER)
    if start == -1 or end == -1:
        raise ValueError(
            "the document has no generated-registry markers; add "
            f"{BEGIN_MARKER} and {END_MARKER} where the table belongs"
        )
    if end < start:
        raise ValueError("the registry markers are in the wrong order")
    return document[:start] + render_registry() + document[end + len(END_MARKER):]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Rewrite the generated identifier registry inside a document"
    )
    parser.add_argument(
        "path", nargs="?", default="docs/GM_ENHANCED_CANDIDATES.md",
        help="the document holding the generated block",
    )
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the document is out of date")
    args = parser.parse_args(argv)

    document = Path(args.path).read_text(encoding="utf-8")
    updated = splice(document)
    if args.check:
        if document != updated:
            print(f"{args.path} is out of date; run this without --check")
            return 1
        print(f"{args.path} is current")
        return 0
    Path(args.path).write_text(updated, encoding="utf-8")
    print(f"{args.path} updated ({len(ENHANCED_READ_DIDS)} identifiers)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
