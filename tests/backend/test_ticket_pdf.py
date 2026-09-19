"""
End-to-end check of the seating plan on the ticket PDF.

The unit tests in test_ticket_image.py call our functions directly. This
module produces real ticket PDFs with pretix's own PDF ticket output and
inspects what actually ended up on the page, because pretix's renderer is
forgiving in a way that hides mistakes: when the image variable fails or
returns nothing, it swallows the error and draws a plain grey rectangle
instead of the plan. Only looking at the PDF shows that.
"""
import io

from pypdf import PdfReader
from pretix.base.models import Order, OrderPosition
from pretix.base.signals import order_placed
from pretix.plugins.ticketoutputpdf.ticketoutput import PdfTicketOutput

from pretix_simpleseatingplan.ticket_image import HIGHLIGHT_FILL

from .test_order_flow import OrderFlowTestCase

# The plan of tests/backend/base.py: 300x200 viewBox, seats a1/a2/a3 at x=50/90/130, y=50.
SEAT_X = {'a1': 50, 'a2': 90, 'a3': 130}
PLAN_W, PLAN_H, SEAT_Y = 300, 200, 50

LAYOUT = [
    {
        'type': 'imagearea', 'left': '10', 'bottom': '20', 'width': '120', 'height': '80',
        'content': 'simpleseating_plan', 'title': 'Seat plan', 'locale': 'en',
    },
    {
        'type': 'textarea', 'left': '10', 'bottom': '110', 'width': '100', 'fontsize': '14', 'lineheight': '1',
        'color': [0, 0, 0, 1], 'fontfamily': 'Open Sans', 'bold': False, 'italic': False, 'width': '100',
        'content': 'other', 'text': 'Concert ticket', 'rotation': 0, 'align': 'left',
    },
]


def is_red(rgb):
    r, g, b = rgb
    return r > 200 and g < 110 and b < 110


def is_green(rgb):
    r, g, b = rgb
    return g > 150 and r < 110 and b < 140


class TicketPdfTests(OrderFlowTestCase):
    def output(self, layout=LAYOUT):
        return PdfTicketOutput(self.event, override_layout=layout)

    def paid_order(self, *labels):
        """A paid order with one position per label, placed the normal way so
        the seats end up assigned."""
        order, first = self.order_with_position(status=Order.STATUS_PAID)
        positions = [first]
        for n in range(1, len(labels)):
            positions.append(OrderPosition.objects.create(
                order=order, item=self.item, price=10, tax_rate=0, tax_value=0, positionid=n + 1))
        for pos, label in zip(positions, labels):
            if label is not None:
                self.answer(pos, self.question, label)
        order_placed.send(sender=self.event, order=order)
        return order, positions

    def page_images(self, pdf_bytes):
        """Every raster image on every page, as PIL images."""
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return [[img.image.convert('RGB') for img in page.images] for page in reader.pages]

    def seat_pixel(self, image, guid):
        return image.getpixel((
            min(image.width - 1, int(image.width * SEAT_X[guid] / PLAN_W)),
            min(image.height - 1, int(image.height * SEAT_Y / PLAN_H)),
        ))

    # ---------------------------------------------------------------------
    def test_ticket_contains_the_plan_with_the_purchased_seat_highlighted(self):
        order, (pos,) = self.paid_order('A-2')
        name, mime, data = self.output().generate(pos)
        self.assertEqual(mime, 'application/pdf')
        self.assertTrue(data.startswith(b'%PDF'))

        (images,) = self.page_images(data)
        self.assertEqual(len(images), 1, 'exactly one raster image: the plan')
        plan = images[0]
        self.assertGreater(plan.width, 800, 'the plan is embedded at a printable resolution')
        self.assertTrue(is_red(self.seat_pixel(plan, 'a2')), 'purchased seat A-2 is highlighted: %r' % (self.seat_pixel(plan, 'a2'),))
        self.assertTrue(is_green(self.seat_pixel(plan, 'a1')), 'A-1 is not: %r' % (self.seat_pixel(plan, 'a1'),))
        self.assertTrue(is_green(self.seat_pixel(plan, 'a3')), 'A-3 is not: %r' % (self.seat_pixel(plan, 'a3'),))

    def test_each_position_of_an_order_gets_its_own_seat_highlighted(self):
        order, positions = self.paid_order('A-1', 'A-3')
        data = self.output().generate_order(order)[2]
        pages = self.page_images(data)
        self.assertEqual(len(pages), 2, 'one page per ticket')
        first, second = (p[0] for p in pages)
        self.assertTrue(is_red(self.seat_pixel(first, 'a1')) and is_green(self.seat_pixel(first, 'a3')))
        self.assertTrue(is_red(self.seat_pixel(second, 'a3')) and is_green(self.seat_pixel(second, 'a1')))

    def test_seat_label_legend_is_drawn_on_the_plan(self):
        order, (pos,) = self.paid_order('A-2')
        (images,) = self.page_images(self.output().generate(pos)[2])
        plan = images[0]
        # The legend is a dark box in the bottom-left corner of the image.
        dark = plan.getpixel((int(plan.width * 0.03), int(plan.height * 0.96)))
        self.assertLess(sum(dark), 200, 'expected the dark legend box, got %r' % (dark,))

    def test_ticket_of_an_unseated_position_shows_the_grey_placeholder_not_a_crash(self):
        order, (pos,) = self.paid_order(None)  # no seat information at all
        with self.assertLogs('pretix_simpleseatingplan.signals', level='WARNING'):
            data = self.output().generate(pos)[2]
        (images,) = self.page_images(data)
        self.assertEqual(images, [], 'no plan image when no seat can be resolved')
        self.assertTrue(data.startswith(b'%PDF'))

    def test_unrelated_layouts_are_unaffected(self):
        order, (pos,) = self.paid_order('A-1')
        text_only = [LAYOUT[1]]
        (images,) = self.page_images(self.output(text_only).generate(pos)[2])
        self.assertEqual(images, [])

    def test_ticket_is_produced_when_the_seat_is_only_known_from_the_answer(self):
        """A PDF preview of an order that has no assignment yet still shows the seat."""
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.answer(pos, self.question, 'a 3')  # order_placed never ran: no SeatAssignment
        (images,) = self.page_images(self.output().generate(pos)[2])
        self.assertTrue(is_red(self.seat_pixel(images[0], 'a3')))

    def test_plan_with_hostile_content_still_prints_and_stays_clean(self):
        from pretix_simpleseatingplan.models import SeatingConfig
        hostile = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 300 200">'
            '<script>alert(1)</script><image xlink:href="file:///etc/passwd" width="10" height="10"/>'
            '<g id="seat-a1" data-seat-label="A-1"><circle cx="50" cy="50" r="12" fill="#22c55e"/></g></svg>'
        )
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg=hostile)
        order, (pos,) = self.paid_order('A-1')
        (images,) = self.page_images(self.output().generate(pos)[2])
        self.assertTrue(is_red(self.seat_pixel(images[0], 'a1')))

    def test_highlight_colour_is_the_documented_one(self):
        self.assertEqual(HIGHLIGHT_FILL, '#ef4444')
