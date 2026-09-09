"""The generated panel has to be the page, pointed somewhere else.

The whole reason this module exists is to avoid a second, drifting copy of
the interface. So the tests care about two things: that the generated file is
the real page rather than a lookalike, and that the address stamped into it
cannot be something a browser will quietly fail to use.
"""

import re
import unittest

from hummer_obd import hapanel

CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
       "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; base-uri 'none'\">")
PAGE = f"<html>\n<head>\n{CSP}\n<title>x</title>\n</head>\n<body>hi</body>\n</html>"


class RenderTests(unittest.TestCase):
    def test_the_api_base_is_stamped_where_the_page_looks_for_it(self):
        out = hapanel.render("http://100.71.118.116:8765", PAGE)
        self.assertIn('<meta name="hummer-api" content="http://100.71.118.116:8765">', out)
        self.assertLess(out.index("hummer-api"), out.index("</head>"),
                        "the tag must be in the head, where the page reads it")

    def test_the_rest_of_the_page_is_untouched(self):
        # The point of generating rather than maintaining: what comes out is
        # what went in, plus an address and the permission to reach it.
        out = hapanel.render("http://node:8765", PAGE)
        self.assertIn("<body>hi</body>", out)
        stripped = out.replace(hapanel.BANNER + "\n", "")
        stripped = stripped.replace(
            '\n<meta name="hummer-api" content="http://node:8765">', "")
        stripped = stripped.replace(" http://node:8765", "")
        self.assertEqual(stripped, PAGE)


class PolicyTests(unittest.TestCase):
    """The page's own CSP has to let it reach the node it was pointed at.

    This is the failure mode that would have shipped: the policy says
    connect-src 'self', which is right while the node serves the page and
    fatal the moment anything else does. Every fetch blocked, the empty state
    rendered, and the only evidence a console message nobody reads.
    """

    @staticmethod
    def policy_of(out):
        """The CSP, not whichever meta tag happens to come first.

        Naively taking the first content= attribute reads the stamped api tag
        instead, which sits above the policy in the head -- a test that then
        reports the widening missing when it is fine.
        """
        return re.search(r'Content-Security-Policy"\s+content="([^"]*)"', out).group(1)

    def test_the_node_is_added_to_connect_src(self):
        out = hapanel.render("http://node:8765", PAGE)
        policy = self.policy_of(out)
        connect = [d for d in policy.split(";") if d.strip().startswith("connect-src")][0]
        self.assertIn("http://node:8765", connect)
        self.assertIn("'self'", connect, "the host's own origin still serves the page")

    def test_the_node_is_added_to_img_src_so_map_tiles_load(self):
        out = hapanel.render("http://node:8765", PAGE)
        policy = self.policy_of(out)
        img = [d for d in policy.split(";") if d.strip().startswith("img-src")][0]
        self.assertIn("http://node:8765", img)
        self.assertIn("data:", img)

    def test_nothing_else_is_relaxed(self):
        # A widening that also loosened script-src or default-src would be
        # buying a map with the page's whole security posture.
        out = hapanel.render("http://node:8765", PAGE)
        policy = self.policy_of(out)
        for directive in policy.split(";"):
            name = directive.strip().split(" ")[0]
            if name not in ("connect-src", "img-src"):
                self.assertNotIn("http://node:8765", directive,
                                 f"{name} was widened and did not need to be")

    def test_a_page_with_no_policy_is_refused_rather_than_shipped_open(self):
        with self.assertRaises(ValueError):
            hapanel.render("http://node:8765", "<html><head></head><body>x</body></html>")

    def test_a_policy_missing_the_directives_is_refused(self):
        page = ('<html><head><meta http-equiv="Content-Security-Policy" '
                'content="default-src \'none\'"></head><body>x</body></html>')
        with self.assertRaises(ValueError):
            hapanel.render("http://node:8765", page)

    def test_the_generated_file_says_it_is_generated(self):
        # Someone will find this file on the Home Assistant box and try to fix
        # a typo in it. It has to tell them where the real one is.
        out = hapanel.render("http://node:8765", PAGE)
        self.assertIn("GENERATED", out)
        self.assertIn("dashboard.html", out)

    def test_a_trailing_slash_does_not_become_a_double_slash(self):
        out = hapanel.render("http://node:8765/", PAGE)
        self.assertIn('content="http://node:8765"', out)

    def test_an_address_a_browser_cannot_use_is_refused(self):
        for bad in ("node:8765",            # no scheme; never matches an Origin
                    "//node:8765",          # protocol-relative
                    "ftp://node",           # not fetchable
                    "http://",              # no host
                    "",
                    "http://node/api",      # a path would be concatenated on
                    "http://node?x=1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hapanel.render(bad, PAGE)

    def test_an_address_that_would_break_out_of_the_attribute_is_refused(self):
        for bad in ('http://node"onload=alert(1)', "http://node<script>"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hapanel.render(bad, PAGE)

    def test_stamping_twice_is_refused_rather_than_producing_two_tags(self):
        once = hapanel.render("http://node:8765", PAGE)
        with self.assertRaises(ValueError):
            hapanel.render("http://other:8765", once)

    def test_a_page_with_no_head_is_refused(self):
        with self.assertRaises(ValueError):
            hapanel.render("http://node:8765", "<html><body>no head</body></html>")


class OriginTests(unittest.TestCase):
    def test_the_origin_to_allow_is_derived_from_the_same_string(self):
        # The node's --allow-origin and the page's api base have to agree
        # exactly or the browser blocks every request with a console message
        # and nothing else. Deriving both from one input is how they agree.
        self.assertEqual(hapanel.origin_of("http://100.64.153.124:8123"),
                         "http://100.64.153.124:8123")
        self.assertEqual(hapanel.origin_of("http://ha.local:8123/"),
                         "http://ha.local:8123")


class RealPageTests(unittest.TestCase):
    def test_the_shipped_page_can_actually_be_stamped(self):
        # Guards the anchor: if dashboard.html ever loses its <head>, or gains
        # a hardcoded api tag, this is the build that breaks rather than a
        # panel that silently reads its own origin and shows nothing.
        out = hapanel.render("http://100.71.118.116:8765")
        self.assertIn('name="hummer-api"', out)
        self.assertIn("<canvas", out, "the rendered panel is not the dashboard")

    def test_the_page_reads_the_tag_this_module_writes(self):
        # Two halves of one contract in two files. Asserted rather than
        # assumed, because a rename on either side fails silently: the panel
        # would load, find no tag, fall back to its own origin, and show an
        # empty dashboard on a host that has no telemetry.
        from importlib.resources import files
        page = files("hummer_obd").joinpath("dashboard.html").read_text(encoding="utf-8")
        self.assertIn('meta[name="hummer-api"]', page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
