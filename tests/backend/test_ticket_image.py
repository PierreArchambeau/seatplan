"""
Tests for the ticket PDF image: the seating plan with the purchased seat
highlighted (ticket_image.py), and how it is fed by the layout image
variable (signals.py).

Rendering is checked on real PNG output, pixel by pixel, not just "some
bytes came out": a highlight in the wrong place is exactly the kind of
mistake that goes unnoticed until tickets are printed.
"""
import hashlib
import io
from unittest import mock

from lxml import etree
from PIL import Image
from pretix.base.models import Order, OrderPosition, QuestionAnswer

from pretix_simpleseatingplan import ticket_image
from pretix_simpleseatingplan.models import Seat, SeatAssignment, SeatingConfig
from pretix_simpleseatingplan.signals import layout_image_variables_handler
from pretix_simpleseatingplan.ticket_image import (
    HIGHLIGHT_FILL, NS_SVG, _add_legend, _canvas_size, _find_seat_element,
    _highlight_element, render_seat_plan_png, resolve_seat_for_position,
)

from .test_order_flow import OrderFlowTestCase

NS = 'xmlns="http://www.w3.org/2000/svg"'


def el(markup):
    return etree.fromstring(markup.encode())


def pixel(png, x, y):
    return Image.open(io.BytesIO(png)).convert('RGB').getpixel((x, y))


def hexrgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class ResolveSeatTests(OrderFlowTestCase):
    def test_prefers_the_recorded_assignment(self):
        order, pos = self.order_with_position()
        SeatAssignment.objects.create(event=self.event, seat_guid='a3', order_position_id=pos.id)
        self.answer(pos, self.question, 'A-1')  # a stale/different answer must not win
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), ('a3', 'A-3'))

    def test_assignment_pointing_at_a_deleted_seat_still_yields_the_guid(self):
        order, pos = self.order_with_position()
        SeatAssignment.objects.create(event=self.event, seat_guid='ghost', order_position_id=pos.id)
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), ('ghost', None))

    def test_falls_back_to_the_typed_answer_when_nothing_is_assigned_yet(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'a 2')
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), ('a2', 'A-2'))

    def test_nothing_resolvable(self):
        order, pos = self.order_with_position()
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), (None, None))
        self.answer(pos, self.question, 'Z-99')
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), (None, None))
        self.answer(pos, self.question, '   ')
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), (None, None))

    def test_without_a_seat_question_only_assignments_count(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-1')
        self.cfg.question_label_id = 0
        self.assertEqual(resolve_seat_for_position(self.event, self.cfg, pos), (None, None))


class ShapeHelpersTests(OrderFlowTestCase):
    def test_find_seat_element_by_prefixed_id_then_by_data_attribute(self):
        root = el('<svg %s><g id="seat-a1"/><g id="x" data-seat-id="u-2"/></svg>' % NS)
        self.assertEqual(_find_seat_element(root, 'seat-', 'a1').get('id'), 'seat-a1')
        self.assertEqual(_find_seat_element(root, 'seat-', 'u-2').get('id'), 'x')
        self.assertIsNone(_find_seat_element(root, 'seat-', 'nope'))

    def test_seat_ids_are_matched_literally_not_as_xpath(self):
        root = el('<svg %s><g id="seat-a1"/></svg>' % NS)
        self.assertIsNone(_find_seat_element(root, 'seat-', "a1' or '1'='1"))

    def test_circle_is_recolored_and_gets_a_dashed_halo(self):
        root = el('<svg %s><circle id="c" cx="10" cy="20" r="5" fill="#22c55e" style="fill:blue"/></svg>' % NS)
        circle = root[0]
        self.assertTrue(_highlight_element(circle))
        self.assertEqual(circle.get('fill'), HIGHLIGHT_FILL)
        self.assertIsNone(circle.get('style'), 'inline style would override the highlight')
        halo = circle.getprevious()
        self.assertEqual(etree.QName(halo).localname, 'circle')
        self.assertEqual((halo.get('cx'), halo.get('cy'), float(halo.get('r'))), ('10.0', '20.0', 11.0))
        self.assertEqual(halo.get('stroke-dasharray'), '4,3')
        self.assertEqual(halo.get('fill'), 'none')

    def test_ellipse_gets_a_scaled_halo(self):
        root = el('<svg %s><ellipse cx="0" cy="0" rx="10" ry="4"/></svg>' % NS)
        ellipse = root[0]
        self.assertTrue(_highlight_element(ellipse))
        halo = ellipse.getprevious()
        self.assertEqual((float(halo.get('rx')), float(halo.get('ry'))), (22.0, 8.8))

    def test_rect_and_path_are_recolored_without_halo(self):
        for shape in ('<rect width="5" height="5"/>', '<path d="M0 0L5 5"/>', '<polygon points="0,0 5,0 5,5"/>'):
            root = el('<svg %s>%s</svg>' % (NS, shape))
            self.assertTrue(_highlight_element(root[0]))
            self.assertEqual(root[0].get('fill'), HIGHLIGHT_FILL)
            self.assertEqual(len(root), 1, 'no halo for %s' % shape)

    def test_group_recolors_every_shape_inside(self):
        root = el('<svg %s><g id="g"><circle cx="1" cy="1" r="1"/><rect width="2" height="2"/><text>1</text></g></svg>' % NS)
        self.assertTrue(_highlight_element(root[0]))
        shapes = [e for e in root.iter() if etree.QName(e).localname in ('circle', 'rect') and not e.get('stroke-dasharray')]
        self.assertEqual([e.get('fill') for e in shapes], [HIGHLIGHT_FILL, HIGHLIGHT_FILL])
        self.assertIsNone(root.xpath('//*[local-name()="text"]')[0].get('fill'))

    def test_element_without_any_shape_is_not_highlighted(self):
        root = el('<svg %s><g id="g"><text>1</text></g></svg>' % NS)
        self.assertFalse(_highlight_element(root[0]))

    def test_garbage_geometry_still_recolors_but_skips_the_halo(self):
        root = el('<svg %s><circle cx="abc" cy="1" r="2"/></svg>' % NS)
        self.assertTrue(_highlight_element(root[0]))
        self.assertEqual(root[0].get('fill'), HIGHLIGHT_FILL)
        self.assertEqual(len(root), 1)
        root = el('<svg %s><ellipse cx="x" cy="1" rx="2" ry="2"/></svg>' % NS)
        self.assertTrue(_highlight_element(root[0]))
        self.assertEqual(len(root), 1)

    def test_canvas_size_from_viewbox_width_height_or_default(self):
        self.assertEqual(_canvas_size(el('<svg %s viewBox="0 0 300 200"/>' % NS)), (300.0, 200.0))
        self.assertEqual(_canvas_size(el('<svg %s viewBox="0,0,640,480"/>' % NS)), (640.0, 480.0))
        self.assertEqual(_canvas_size(el('<svg %s viewBox="junk" width="120" height="80"/>' % NS)), (120.0, 80.0))
        self.assertEqual(_canvas_size(el('<svg %s viewBox="0 0 a b" width="x"/>' % NS)), (900.0, 900.0))
        self.assertEqual(_canvas_size(el('<svg %s/>' % NS)), (900.0, 900.0))

    def test_legend_is_added_at_the_bottom_left_with_the_seat_label(self):
        root = el('<svg %s viewBox="0 0 1000 500"/>' % NS)
        _add_legend(root, 'A-12')
        rect, text = root
        self.assertEqual(text.text, 'A-12')
        self.assertLess(float(rect.get('x')), 100)
        self.assertGreater(float(rect.get('y')), 400)  # near the bottom edge
        self.assertEqual(text.get('fill'), '#ffffff')


class RenderTests(OrderFlowTestCase):
    def render(self, svg=None, guid='a2', label='A-2', **kwargs):
        if svg is not None:
            SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg=svg)
            self.cfg.refresh_from_db()
        return render_seat_plan_png(self.event, self.cfg, guid, label, **kwargs)

    def test_renders_a_png_of_the_requested_width_with_only_the_chosen_seat_highlighted(self):
        png = self.render()  # base plan: 300x200, seats at x=50/90/130, y=50, r=12
        self.assertTrue(png.startswith(b'\x89PNG'))
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.width, 1600)
        scale = 1600 / 300
        red = hexrgb(HIGHLIGHT_FILL)
        faded_green = tuple(round(0.3 * c + 0.7 * 255) for c in hexrgb('#22c55e'))  # 30 % opacity on white
        self.assertEqual(pixel(png, int(90 * scale), int(50 * scale)), red, 'chosen seat A-2 is highlighted')
        for x in (50, 130):
            got = pixel(png, int(x * scale), int(50 * scale))
            self.assertLess(distance(got, faded_green), 8, 'the other seats are faded on purpose: %r' % (got,))

    def test_highlighting_a_different_seat_moves_the_highlight(self):
        png = self.render(guid='a3', label='A-3')
        scale = 1600 / 300
        self.assertEqual(pixel(png, int(130 * scale), int(50 * scale)), hexrgb(HIGHLIGHT_FILL))
        self.assertGreater(pixel(png, int(90 * scale), int(50 * scale))[0], 150, 'A-2 is faded, no longer highlighted')

    def test_output_width_is_configurable(self):
        png = self.render(output_width=400)
        self.assertEqual(Image.open(io.BytesIO(png)).width, 400)

    def test_legend_with_the_seat_label_is_drawn_when_a_label_is_given(self):
        with_label = self.render(label='A-2')
        without_label = self.render(label=None)
        self.assertNotEqual(with_label, without_label)

    def test_seat_defined_through_data_seat_id_is_found(self):
        svg = ('<svg %s viewBox="0 0 100 100"><g id="whatever" data-seat-id="a2">'
               '<circle cx="50" cy="50" r="20" fill="#22c55e"/></g></svg>' % NS)
        png = self.render(svg=svg)
        self.assertEqual(pixel(png, int(50 * 16), int(50 * 16)), hexrgb(HIGHLIGHT_FILL))

    def test_no_plan_returns_none(self):
        self.assertIsNone(self.render(svg=''))

    def test_unusable_plan_returns_none_instead_of_raising(self):
        self.assertIsNone(self.render(svg='<html><body>not a plan</body></html>'))

    def test_seat_missing_from_the_plan_returns_none(self):
        self.assertIsNone(self.render(guid='not-in-plan', label='Z-9'))

    def test_seat_without_any_shape_returns_none(self):
        svg = '<svg %s viewBox="0 0 10 10"><g id="seat-a2"><text>2</text></g></svg>' % NS
        self.assertIsNone(self.render(svg=svg))

    def test_a_failing_renderer_returns_none_and_is_logged(self):
        with mock.patch('cairosvg.svg2png', side_effect=RuntimeError('cairo exploded')):
            with self.assertLogs('pretix_simpleseatingplan.ticket_image', level='ERROR'):
                self.assertIsNone(self.render())

    def test_hostile_plan_content_never_reaches_the_renderer(self):
        svg = ('<svg %s xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">'
               '<image xlink:href="file:///etc/passwd" width="10" height="10"/>'
               '<g id="seat-a2"><circle cx="50" cy="50" r="20"/></g></svg>' % NS)
        with mock.patch('cairosvg.svg2png', wraps=__import__('cairosvg').svg2png) as spy:
            png = self.render(svg=svg)
        self.assertIsNotNone(png)
        rendered = spy.call_args.kwargs['bytestring'].decode()
        self.assertNotIn('passwd', rendered)
        self.assertNotIn('file://', rendered)


def luminance(rgb):
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


class HighlightVisibilityTests(OrderFlowTestCase):
    """The purchased seat must stand out whatever colours the plan uses.

    Reported from production: every seat of the plan is red, and the highlight
    was red too, so the customer's seat could not be told from the others.
    The check is on real pixels: the chosen seat against EVERY other seat, in
    colour and in grey scale (tickets are often printed in black and white)."""

    SEAT_COUNT = 8
    CHOSEN = 3

    def plan(self, fill, extra=''):
        seats = ''.join(
            '<g id="seat-a%d" data-seat-label="A-%d"><circle cx="%d" cy="50" r="12" fill="%s" stroke="#333" stroke-width="1"/></g>'
            % (i, i, 30 + 40 * i, fill, ) for i in range(self.SEAT_COUNT))
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 100">%s%s</svg>' % (extra, seats)

    def seat_pixels(self, svg, width=1600):
        self.cfg.svg = svg
        png = render_seat_plan_png(self.event, self.cfg, 'a%d' % self.CHOSEN, 'A-%d' % self.CHOSEN, output_width=width)
        self.assertIsNotNone(png)
        scale = width / 400
        return [pixel(png, int((30 + 40 * i) * scale), int(50 * scale)) for i in range(self.SEAT_COUNT)], png

    def assertStandsOut(self, fill):
        pixels, _png = self.seat_pixels(self.plan(fill))
        chosen = pixels[self.CHOSEN]
        for i, other in enumerate(pixels):
            if i == self.CHOSEN:
                continue
            self.assertGreaterEqual(distance(chosen, other), 120, 'seats coloured %s: chosen %r vs seat %d %r' % (fill, chosen, i, other))
            self.assertGreaterEqual(abs(luminance(chosen) - luminance(other)), 40,
                                    'seats coloured %s would not be told apart in black and white: %r vs %r' % (fill, chosen, other))

    def test_when_every_seat_of_the_plan_is_red_the_chosen_one_still_stands_out(self):
        for red in ('#ef4444', '#dc2626', '#ff0000', 'red', '#b91c1c'):
            self.assertStandsOut(red)

    def test_it_stands_out_for_the_other_common_seat_colours_too(self):
        for colour in ('#22c55e', '#2563eb', '#f59e0b', '#a855f7', '#000000', '#6b7280', '#ffffff', '#facc15', '#f97316'):
            self.assertStandsOut(colour)

    def test_the_other_seats_are_faded_not_removed(self):
        pixels, _png = self.seat_pixels(self.plan('#22c55e'))
        others = [p for i, p in enumerate(pixels) if i != self.CHOSEN]
        for p in others:
            self.assertGreater(p[0], 140, 'faded towards white: %r' % (p,))
            self.assertGreater(p[1] - p[0], 20, 'but still recognisably green: %r' % (p,))

    def test_the_parts_of_the_plan_that_are_not_seats_keep_their_colour(self):
        stage = '<rect id="stage" x="150" y="80" width="100" height="15" fill="#111111"/>'
        self.cfg.svg = self.plan('#22c55e', extra=stage)
        png = render_seat_plan_png(self.event, self.cfg, 'a3', 'A-3')
        self.assertLess(sum(pixel(png, int(200 * 4), int(88 * 4))), 120, 'the stage is not faded')

    def test_seats_identified_only_by_data_seat_id_are_faded_as_well(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
               '<g id="x1" data-seat-id="a1"><circle cx="20" cy="25" r="10" fill="#22c55e"/></g>'
               '<g id="x2" data-seat-id="a2"><circle cx="60" cy="25" r="10" fill="#22c55e"/></g></svg>')
        self.cfg.svg = svg
        png = render_seat_plan_png(self.event, self.cfg, 'a1', 'A-1', output_width=1000)
        self.assertGreater(pixel(png, 600, 250)[0], 140, 'the other seat is faded')

    def test_a_reddish_seat_gets_the_alternative_highlight_colour(self):
        from pretix_simpleseatingplan import ticket_image as ti
        for fill in ('#ef4444', 'red', '#f00', 'rgb(220, 38, 38)', 'crimson', '#b91c1c'):
            root = el('<svg %s><circle cx="1" cy="1" r="2" fill="%s"/></svg>' % (NS, fill))
            seat = root[0]
            ti._highlight_element(seat)
            self.assertEqual(seat.get('fill'), ti.HIGHLIGHT_FILL_ALT, fill)

    def test_a_seat_coloured_through_a_style_attribute_is_recognised_too(self):
        from pretix_simpleseatingplan import ticket_image as ti
        root = el('<svg %s><circle cx="1" cy="1" r="2" style="fill:#dc2626;stroke:#000"/></svg>' % NS)
        seat = root[0]
        ti._highlight_element(seat)
        self.assertEqual(seat.get('fill'), ti.HIGHLIGHT_FILL_ALT)

    def test_other_seat_colours_keep_the_standard_highlight(self):
        from pretix_simpleseatingplan import ticket_image as ti
        for fill in ('#22c55e', '#2563eb', '#f59e0b', 'none', 'url(#gradient)', '#000000', '#ffffff'):
            root = el('<svg %s><circle cx="1" cy="1" r="2" fill="%s"/></svg>' % (NS, fill))
            seat = root[0]
            ti._highlight_element(seat)
            self.assertEqual(seat.get('fill'), ti.HIGHLIGHT_FILL, fill)

    def test_a_group_of_shapes_is_judged_by_its_first_coloured_shape(self):
        from pretix_simpleseatingplan import ticket_image as ti
        root = el('<svg %s><g id="g"><rect width="4" height="4" fill="#dc2626"/><circle r="1" fill="#22c55e"/></g></svg>' % NS)
        group, rect = root[0], root[0][0]
        ti._highlight_element(group)
        self.assertEqual(rect.get('fill'), ti.HIGHLIGHT_FILL_ALT)


class LayoutImageVariableTests(OrderFlowTestCase):
    def variable(self):
        return layout_image_variables_handler(self.event)['simpleseating_plan']

    def order_with_seat(self, guid='a2'):
        order, pos = self.order_with_position()
        SeatAssignment.objects.create(event=self.event, seat_guid=guid, order_position_id=pos.id)
        return order, pos

    def test_is_registered_with_a_label_and_two_callables(self):
        var = self.variable()
        self.assertTrue(str(var['label']))
        self.assertTrue(callable(var['evaluate']) and callable(var['etag']))

    def test_evaluate_returns_a_named_png_file_for_a_seated_position(self):
        order, pos = self.order_with_seat('a2')
        content = self.variable()['evaluate'](pos, order, self.event)
        self.assertEqual(content.name, 'seatplan-a2.png')
        self.assertTrue(content.read().startswith(b'\x89PNG'))

    def test_evaluate_is_none_when_there_is_no_seat_for_the_position(self):
        order, pos = self.order_with_position()
        with self.assertLogs('pretix_simpleseatingplan.signals', level='WARNING'):
            self.assertIsNone(self.variable()['evaluate'](pos, order, self.event))

    def test_evaluate_is_none_for_positions_of_another_item(self):
        from pretix.base.models import Item
        other_item = Item.objects.create(event=self.event, name='Parking', default_price=1, active=True)
        order, _ = self.order_with_position()
        pos = OrderPosition.objects.create(order=order, item=other_item, price=1, tax_rate=0, tax_value=0, positionid=2)
        SeatAssignment.objects.create(event=self.event, seat_guid='a1', order_position_id=pos.id)
        self.assertIsNone(self.variable()['evaluate'](pos, order, self.event))
        self.assertIsNone(self.variable()['etag'](pos, order, self.event))

    def test_evaluate_is_none_without_a_plan_or_a_configuration(self):
        order, pos = self.order_with_seat()
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg='')
        self.assertIsNone(self.variable()['evaluate'](pos, order, self.event))
        SeatingConfig.objects.all().delete()
        self.assertIsNone(self.variable()['evaluate'](pos, order, self.event))
        self.assertIsNone(self.variable()['etag'](pos, order, self.event))

    def test_evaluate_is_none_when_the_seat_cannot_be_drawn(self):
        order, pos = self.order_with_seat('a2')
        with mock.patch('pretix_simpleseatingplan.signals.render_seat_plan_png', return_value=None):
            self.assertIsNone(self.variable()['evaluate'](pos, order, self.event))

    def test_etag_is_stable_and_changes_with_the_seat_and_the_plan(self):
        order, pos = self.order_with_seat('a2')
        etag = self.variable()['etag']
        first = etag(pos, order, self.event)
        self.assertEqual(first, hashlib.sha1(('a2|' + self.cfg.svg).encode()).hexdigest())
        self.assertEqual(first, etag(pos, order, self.event))

        SeatAssignment.objects.filter(order_position_id=pos.id).update(seat_guid='a3')
        self.assertNotEqual(first, etag(pos, order, self.event))

        SeatAssignment.objects.filter(order_position_id=pos.id).update(seat_guid='a2')
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg=self.cfg.svg.replace('#22c55e', '#111111'))
        self.assertNotEqual(first, etag(pos, order, self.event))

    def test_etag_is_none_when_no_seat_resolves(self):
        order, pos = self.order_with_position()
        self.assertIsNone(self.variable()['etag'](pos, order, self.event))
