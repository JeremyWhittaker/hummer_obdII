"""The page's script must parse. Nothing else in this suite checks that.

On 2026-09-10 a part-index entry was written with Python's implicit string
concatenation -- two adjacent string literals on consecutive lines, with no
`+` between them. Python joins those silently. JavaScript does not: it is a
SyntaxError, and a SyntaxError in the single inline <script> takes the whole
block with it.

The result was a page that loaded, styled itself correctly, showed every
heading and card, reported no console error the extension could see, and
rendered nothing at all. The canvas sat at its default 300x150 because the
code that sizes it never ran. It shipped, and it survived a deploy and a
screenshot, because everything a human glance checks still looked right.

1179 tests passed the whole time. They test Python.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

PAGE = files("hummer_obd").joinpath("dashboard.html").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def scripts() -> list[str]:
    return re.findall(r"<script[^>]*>([\s\S]*?)</script>", PAGE)


class ScriptParsesTests(unittest.TestCase):
    def test_the_page_has_exactly_one_inline_script(self):
        # If this ever becomes more than one, the check below must cover them
        # all -- a second block failing would be just as invisible.
        self.assertEqual(len(scripts()), 1)

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_the_script_parses_as_javascript(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.js"
            path.write_text(scripts()[0], encoding="utf-8")
            done = subprocess.run([NODE, "--check", str(path)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0,
                         f"dashboard.html's script does not parse:\n{done.stderr}")


class ImplicitConcatenationTests(unittest.TestCase):
    """The specific mistake, caught by shape rather than by running node.

    Kept separate so it still guards on a machine with no node, and because
    naming the failure is worth more than a generic parse error would be.
    """

    #: A line ending in a string literal, followed by a line whose first
    #: non-space character opens another one. Valid Python, broken JavaScript.
    PATTERN = re.compile(r'"\s*\n\s*"')

    def test_no_two_string_literals_are_juxtaposed(self):
        source = scripts()[0]
        offenders = []
        for match in self.PATTERN.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            context = source[max(0, match.start() - 70):match.end() + 70]
            # A comma, operator or bracket between them is ordinary code
            # spanning lines; only a bare literal-then-literal is the bug.
            before = source[:match.start()].rstrip()
            if before.endswith('"') and not before.endswith('\\"'):
                offenders.append((line, context.replace("\n", " / ")))
        self.assertEqual(
            offenders, [],
            "adjacent string literals with no '+' -- Python concatenates "
            f"these, JavaScript does not: {offenders[:3]}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
