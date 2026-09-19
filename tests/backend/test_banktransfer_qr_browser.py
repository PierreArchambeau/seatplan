"""
The bank transfer QR code, as a real browser displays it.

test_banktransfer_qr.py proves the server sends the right payload. The image
itself is drawn in the browser by pretix's JavaScript, so this module starts
pretix on a real HTTP port, loads a real order page in headless Edge/Chrome,
takes a screenshot and DECODES the QR code that is on it, checking that what a
customer's banking app would read is exactly the expected EPC payment payload.

Skipped automatically when no Chromium-based browser or no QR decoder
(`pip install zxing-cpp`) is available. Set SEATPLAN_SKIP_BROWSER_TESTS=1 to
skip it on purpose.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings
from PIL import Image
from pretix.base.models import Order, Team

from .base import SeatingFixtures

try:
    import zxingcpp
except ImportError:  # pragma: no cover
    zxingcpp = None

QR_SCRIPT = re.compile(
    r'<script type="application/json" data-size="150" data-replace-with-qr[^>]*>(.*?)</script>', re.S)


def find_browser():
    candidates = [
        os.environ.get('SEATPLAN_TEST_BROWSER'),
        shutil.which('msedge'), shutil.which('chrome'), shutil.which('google-chrome'), shutil.which('chromium'),
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


BROWSER = find_browser()


@unittest.skipIf(os.environ.get('SEATPLAN_SKIP_BROWSER_TESTS'), 'browser tests disabled by SEATPLAN_SKIP_BROWSER_TESTS')
@unittest.skipUnless(BROWSER, 'needs Edge or Chrome (or set SEATPLAN_TEST_BROWSER)')
@unittest.skipUnless(zxingcpp, 'needs a QR decoder: pip install zxing-cpp')
class BankTransferQrInBrowserTests(SeatingFixtures, StaticLiveServerTestCase):
    NAME = 'Ticket Shop ASBL'
    IBAN = 'DE02120300000000202051'
    BIC = 'BELADEBEXXX'
    BANK = 'Landesbank Berlin'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Serve pretix under the address the browser will use.
        cls._site = override_settings(SITE_URL=cls.live_server_url, ALLOWED_HOSTS=['*'])
        cls._site.enable()

    @classmethod
    def tearDownClass(cls):
        cls._site.disable()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.event.plugins += ',pretix.plugins.banktransfer'
        self.event.save()
        self.organizer.plugins = (self.organizer.plugins or '') + ',pretix.plugins.banktransfer'
        self.organizer.save()
        for key, value in {
            '_enabled': True, 'bank_details_type': 'sepa', 'bank_details_sepa_name': self.NAME,
            'bank_details_sepa_iban': self.IBAN, 'bank_details_sepa_bic': self.BIC,
            'bank_details_sepa_bank': self.BANK,
        }.items():
            self.event.settings.set('payment_banktransfer_' + key, value)
        team = Team.objects.create(organizer=self.organizer, name='API', all_events=True, all_event_permissions=True)
        payload = {
            'email': 'buyer@example.org', 'locale': 'en', 'sales_channel': 'web', 'payment_provider': 'banktransfer',
            'positions': [{'item': self.item.id, 'answers': [
                {'question': self.question.id, 'answer': 'A-2', 'options': []}]}],
        }
        resp = Client().post(
            '/api/v1/organizers/org/events/concert/orders/', data=json.dumps(payload),
            content_type='application/json', HTTP_AUTHORIZATION='Token ' + team.tokens.create(name='t').token,
        )
        assert resp.status_code == 201, resp.content
        self.order = Order.objects.get(code=resp.json()['code'])
        self.url = '%s/org/concert/order/%s/%s/' % (self.live_server_url, self.order.code, self.order.secret)

    # -- helpers --------------------------------------------------------------
    def run_browser(self, *extra):
        profile = tempfile.mkdtemp(prefix='seatplan-browser-')
        try:
            args = [
                BROWSER, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                '--hide-scrollbars', '--force-device-scale-factor=2', '--window-size=1200,2600',
                '--virtual-time-budget=15000', '--user-data-dir=' + profile, *extra, self.url,
            ]
            return subprocess.run(args, capture_output=True, text=True, timeout=120)
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    def server_payloads(self):
        html = Client().get('/org/concert/order/%s/%s/' % (self.order.code, self.order.secret)).content.decode()
        return [json.loads(raw) for raw in QR_SCRIPT.findall(html)]

    # -- tests ----------------------------------------------------------------
    def test_javascript_replaces_each_payload_with_a_labelled_canvas(self):
        result = self.run_browser('--dump-dom')
        dom = result.stdout
        self.assertIn('Please transfer the full amount', dom, result.stderr[-500:])
        canvases = re.findall(r'<canvas[^>]*role="img"[^>]*>', dom)
        self.assertEqual(len(canvases), len(self.server_payloads()), 'one drawn QR code per payload')
        for canvas in canvases:
            self.assertIn('aria-label="Scan this image with your banking app', canvas)

    def test_the_qr_code_on_screen_decodes_to_the_expected_epc_payload(self):
        shot = Path(tempfile.mkdtemp(prefix='seatplan-shot-')) / 'order.png'
        try:
            result = self.run_browser('--screenshot=' + str(shot))
            self.assertTrue(shot.exists(), 'no screenshot: %s' % result.stderr[-500:])
            image = Image.open(shot).convert('RGB')
            if os.environ.get('SEATPLAN_KEEP_SCREENSHOT'):  # to look at what the browser really showed
                shutil.copy(shot, os.environ['SEATPLAN_KEEP_SCREENSHOT'])
            decoded = [b for b in zxingcpp.read_barcodes(image) if 'QR' in str(b.format)]
            self.assertGreaterEqual(len(decoded), 1, 'no QR code could be read from the page (%dx%d)' % image.size)
            expected = next(p for p in self.server_payloads() if p.startswith('BCD'))
            self.assertIn(expected, [b.text for b in decoded], 'the QR on screen is not the EPC payment payload')
        finally:
            shutil.rmtree(shot.parent, ignore_errors=True)

    def test_the_decoded_qr_is_a_well_formed_transfer_to_the_right_account(self):
        shot = Path(tempfile.mkdtemp(prefix='seatplan-shot-')) / 'order.png'
        try:
            self.run_browser('--screenshot=' + str(shot))
            texts = [b.text for b in zxingcpp.read_barcodes(Image.open(shot).convert('RGB'))]
            lines = next(t for t in texts if t.startswith('BCD')).split('\n')
            self.assertEqual(lines[4], self.BIC)
            self.assertEqual(lines[5], self.NAME)
            self.assertEqual(lines[6], self.IBAN)
            self.assertEqual(lines[7], 'EUR10.00')
            self.assertEqual(lines[10], 'CONCERT-' + self.order.code)
        finally:
            shutil.rmtree(shot.parent, ignore_errors=True)


class LionsClubHuyInBrowserTests(BankTransferQrInBrowserTests):
    """Same checks, with the real Belgian account: what a customer's banking app
    would read from the screen must be this transfer, to this account."""
    NAME = 'asbl Lions International Club de Huy'
    IBAN = 'BE47240085008780'
    BIC = 'GEBABEBBXXX'
    BANK = 'BNP Paribas Fortis'

    def test_only_one_qr_code_is_drawn_for_a_belgian_account(self):
        dom = self.run_browser('--dump-dom').stdout
        self.assertEqual(len(re.findall(r'<canvas[^>]*role="img"[^>]*>', dom)), 1)
