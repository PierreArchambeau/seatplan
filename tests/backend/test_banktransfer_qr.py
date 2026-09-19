"""
The payment QR code shown to a customer who pays by bank transfer.

This is pretix core behaviour, not something this plugin implements, but it
sits on the same order page and checkout as the seat picker, so it is worth
proving that the two coexist: with the plugin enabled, a real order paid by
bank transfer must still show the payment details and the QR code data.

Server side, pretix only emits the payload
(`<script type="application/json" data-replace-with-qr>`); the QR image itself
is drawn in the browser by pretix's JavaScript. This module covers the server
half against the real order page; see tests/js for the front-end half.
"""
import json
import re
from decimal import Decimal
from html import unescape
from urllib.parse import parse_qs, urlsplit

from django.test import Client
from pretix.base.models import Order, Team
from reportlab.graphics.barcode.qr import QrCodeWidget

from .base import SeatingTestCase

IBAN_DE = 'DE02120300000000202051'
QR_SCRIPT = re.compile(
    r'<script type="application/json" data-size="150" data-replace-with-qr[^>]*>(.*?)</script>', re.S)


def iban_is_valid(iban):
    iban = iban.replace(' ', '').upper()
    rearranged = iban[4:] + iban[:4]
    digits = ''.join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


class BankTransferHelpers:
    """Order paid by bank transfer, order page, QR payloads. The account is
    described by class attributes so several accounts can reuse the tests."""
    NAME = 'Ticket Shop ASBL'
    IBAN = IBAN_DE            # as pretix stores it: without spaces
    BIC = 'BELADEBEXXX'
    BANK = 'Landesbank Berlin'

    def setUp(self):
        super().setUp()
        # The bank transfer plugin is "hybrid": it must be enabled for the organizer as well as the event.
        self.event.plugins += ',pretix.plugins.banktransfer'
        self.event.save()
        self.organizer.plugins = (self.organizer.plugins or '') + ',pretix.plugins.banktransfer'
        self.organizer.save()
        self.configure_bank()
        team = Team.objects.create(organizer=self.organizer, name='API', all_events=True, all_event_permissions=True)
        self.token = team.tokens.create(name='qr-test').token

    def configure_bank(self, **overrides):
        settings = {
            '_enabled': True,
            'bank_details_type': 'sepa',
            'bank_details_sepa_name': self.NAME,
            'bank_details_sepa_iban': self.IBAN,
            'bank_details_sepa_bic': self.BIC,
            'bank_details_sepa_bank': self.BANK,
        }
        settings.update(overrides)
        for key, value in settings.items():
            self.event.settings.set('payment_banktransfer_' + key, value)

    def order_by_bank_transfer(self, seat_label='A-2'):
        payload = {
            'email': 'buyer@example.org', 'locale': 'en', 'sales_channel': 'web',
            'payment_provider': 'banktransfer',
            'positions': [{
                'item': self.item.id,
                'answers': [{'question': self.question.id, 'answer': seat_label, 'options': []}],
            }],
        }
        resp = Client().post(
            '/api/v1/organizers/org/events/concert/orders/', data=json.dumps(payload),
            content_type='application/json', HTTP_AUTHORIZATION='Token ' + self.token,
        )
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        return Order.objects.get(code=resp.json()['code'])

    def order_page(self, order):
        resp = Client().get('/org/concert/order/%s/%s/' % (order.code, order.secret))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def qr_payloads(self, html):
        return [json.loads(raw) for raw in QR_SCRIPT.findall(html)]


class BankTransferQrTests(BankTransferHelpers, SeatingTestCase):
    """A German account: EPC QR plus the German-only BezahlCode."""

    # ---------------------------------------------------------------------
    def test_order_page_shows_the_bank_details_and_a_qr_payload(self):
        order = self.order_by_bank_transfer()
        html = self.order_page(order)
        self.assertIn('Please transfer the full amount', html)
        self.assertIn(order.code, html, 'the reference the customer must use')
        self.assertIn('Ticket Shop ASBL', html)
        self.assertIn('DE02 1203 0000 0000 2020 51', html)
        self.assertEqual(html.count('data-replace-with-qr'), len(self.qr_payloads(html)))
        self.assertGreaterEqual(len(self.qr_payloads(html)), 1, 'a QR code payload is present')

    def test_qr_payload_is_a_valid_epc_giro_code_for_this_order(self):
        order = self.order_by_bank_transfer()
        epc = next(p for p in self.qr_payloads(self.order_page(order)) if p.startswith('BCD'))
        lines = epc.split('\n')
        self.assertEqual(lines[0:4], ['BCD', '002', '2', 'SCT'], 'service tag, version 2, UTF-8, credit transfer')
        self.assertEqual(lines[4], 'BELADEBEXXX')
        self.assertEqual(lines[5], 'Ticket Shop ASBL')
        self.assertEqual(lines[6], IBAN_DE)
        self.assertTrue(iban_is_valid(lines[6]))
        self.assertEqual(lines[7], 'EUR%s' % Decimal(order.total).quantize(Decimal('0.01')))
        self.assertIn(order.code, lines[10], 'the payment reference carries the order code')
        self.assertLessEqual(len(epc.encode('utf-8')), 331, 'EPC069-12 size limit')
        self.assertLessEqual(len(lines[5]), 70)

    def test_qr_payload_fits_in_a_qr_code_at_the_error_level_pretix_uses(self):
        order = self.order_by_bank_transfer()
        for payload in self.qr_payloads(self.order_page(order)):
            widget = QrCodeWidget(payload, barLevel='M')  # raises if it does not fit
            self.assertGreater(widget.getBounds()[2], 0)

    def test_german_iban_also_offers_a_bezahlcode_tab_with_a_working_link(self):
        order = self.order_by_bank_transfer()
        html = self.order_page(order)
        self.assertIn('BezahlCode', html)
        self.assertIn('EPC-QR', html)
        link = unescape(re.search(r'href="(bank://[^"]+)"', html).group(1))
        params = parse_qs(urlsplit(link).query)
        self.assertEqual(params['iban'], [IBAN_DE])
        self.assertEqual(params['bic'], ['BELADEBEXXX'])
        self.assertEqual(params['name'], ['Ticket Shop ASBL'])
        self.assertEqual(params['currency'], ['EUR'])
        self.assertEqual(params['amount'], [str(Decimal(order.total).quantize(Decimal('0.01'))).replace('.', ',')])
        # pretix prefixes the reference with the event slug; the order code must be in it
        self.assertEqual(params['reason'], ['CONCERT-' + order.code])

    def test_qr_container_is_meant_for_javascript_and_has_an_accessible_label(self):
        html = self.order_page(self.order_by_bank_transfer())
        self.assertIn('js-only', html)
        self.assertIn('role="figure"', html)
        self.assertIn('Scan this image with your banking app', html)
        self.assertIn('Scan the QR code with your banking app', html)

    def test_the_seat_question_does_not_disturb_the_payment_block(self):
        """The order carries a seat answer handled by this plugin; the payment
        block must be rendered exactly as it is without it."""
        order = self.order_by_bank_transfer(seat_label='A-3')
        html = self.order_page(order)
        self.assertGreaterEqual(len(self.qr_payloads(html)), 1)
        self.assertIn('A-3', html, 'the seat answer is shown with the order')

    # -- no QR when it would be wrong -----------------------------------------
    def test_no_qr_code_without_an_iban(self):
        self.configure_bank(bank_details_sepa_iban='')
        html = self.order_page(self.order_by_bank_transfer())
        self.assertEqual(self.qr_payloads(html), [])

    def test_no_qr_code_when_bank_details_are_free_text_only(self):
        self.configure_bank(bank_details_type='other', bank_details='Pay to our account, see invoice')
        html = self.order_page(self.order_by_bank_transfer())
        self.assertEqual(self.qr_payloads(html), [])
        self.assertIn('Pay to our account', html)

    def test_no_epc_qr_for_a_non_euro_event(self):
        self.event.currency = 'USD'
        self.event.save()
        html = self.order_page(self.order_by_bank_transfer())
        self.assertEqual(self.qr_payloads(html), [])

    def test_no_qr_code_once_the_order_is_paid(self):
        order = self.order_by_bank_transfer()
        Order.objects.filter(pk=order.pk).update(status=Order.STATUS_PAID)
        order.payments.update(state='confirmed')
        html = self.order_page(order)
        self.assertEqual(self.qr_payloads(html), [])


class LionsClubHuyTransferTests(BankTransferHelpers, SeatingTestCase):
    """The real account of a Belgian non-profit (BNP Paribas Fortis): EPC QR
    only -- BezahlCode is German-only and the Swiss QR-bill needs a CH/LI IBAN."""
    NAME = 'asbl Lions International Club de Huy'
    IBAN = 'BE47240085008780'          # entered as "BE47 2400 8500 8780"; pretix stores it without spaces
    BIC = 'GEBABEBBXXX'
    BANK = 'BNP Paribas Fortis'

    def epc_lines(self, order):
        epc = next(p for p in self.qr_payloads(self.order_page(order)) if p.startswith('BCD'))
        return epc, epc.split('\n')

    def test_the_iban_and_bic_are_valid(self):
        """Guards the fixture itself: a typo here would make every test below meaningless."""
        self.assertTrue(iban_is_valid('BE47 2400 8500 8780'))
        digits = self.IBAN[4:]
        self.assertEqual(int(digits[:10]) % 97 or 97, int(digits[10:]), 'Belgian national check digits')
        self.assertEqual((len(self.IBAN), len(self.BIC)), (16, 11))

    def test_order_page_shows_the_account_the_way_a_customer_expects_to_read_it(self):
        order = self.order_by_bank_transfer()
        html = self.order_page(order)
        for expected in (
            'asbl Lions International Club de Huy', 'BE47 2400 8500 8780', 'GEBABEBBXXX', 'BNP Paribas Fortis',
            'CONCERT-' + order.code, '10.00',
        ):
            self.assertIn(expected, html)

    def test_exactly_one_qr_code_is_offered_and_it_is_the_epc_one(self):
        order = self.order_by_bank_transfer()
        html = self.order_page(order)
        self.assertEqual(len(self.qr_payloads(html)), 1)
        self.assertIn('EPC-QR', html)
        for other in ('BezahlCode', 'QR-bill', 'bank://', 'SPAYD'):
            self.assertNotIn(other, html)

    def test_epc_payload_matches_the_account_line_by_line(self):
        order = self.order_by_bank_transfer()
        epc, lines = self.epc_lines(order)
        self.assertEqual(lines[:4], ['BCD', '002', '2', 'SCT'])
        self.assertEqual(lines[4], 'GEBABEBBXXX', 'BIC of the beneficiary bank')
        self.assertEqual(lines[5], 'asbl Lions International Club de Huy', 'beneficiary name')
        self.assertEqual(lines[6], 'BE47240085008780', 'IBAN without spaces, as the EPC standard requires')
        self.assertEqual(lines[7], 'EUR10.00')
        self.assertEqual(lines[8:10], ['', ''])
        self.assertEqual(lines[10], 'CONCERT-' + order.code, 'unstructured reference the bank import matches on')
        self.assertLessEqual(len(epc.encode('utf-8')), 331)
        self.assertLessEqual(len(lines[5]), 70)
        self.assertNotIn(' ', lines[6])

    def test_amount_follows_the_ticket_price(self):
        from pretix.base.models import Item
        Item.objects.filter(pk=self.item.pk).update(default_price=Decimal('12.50'))
        order = self.order_by_bank_transfer()
        self.assertEqual(self.epc_lines(order)[1][7], 'EUR12.50')

    def test_payload_fits_in_a_qr_code_at_error_level_m(self):
        order = self.order_by_bank_transfer()
        widget = QrCodeWidget(self.epc_lines(order)[0], barLevel='M')
        self.assertGreater(widget.getBounds()[2], 0)

    def test_the_seat_answer_of_the_order_does_not_change_the_payment(self):
        first = self.epc_lines(self.order_by_bank_transfer(seat_label='A-1'))[1]
        second = self.epc_lines(self.order_by_bank_transfer(seat_label='A-3'))[1]
        self.assertEqual(first[:10], second[:10])
        self.assertNotEqual(first[10], second[10], 'each order has its own reference')
