"""
The seat audit page lists order codes and what customers typed in their
orders, and its "fix" button creates seat assignments. Being allowed to
change the event *settings* must not be enough to see or change orders.
"""
from pretix.base.models import Order, OrderPosition, QuestionAnswer, SalesChannel
from django.utils import timezone
import datetime

from pretix_simpleseatingplan.models import SeatAssignment

from .base import SeatingTestCase

SETTINGS = 'event.settings.general:write'
ORDERS_READ = 'event.orders:read'
ORDERS_WRITE = 'event.orders:write'


class AuditPermissionTests(SeatingTestCase):
    def setUp(self):
        super().setUp()
        channel = SalesChannel.objects.get(organizer=self.organizer, identifier='web')
        self.order = Order.objects.create(
            event=self.event, organizer=self.organizer, code='AUDIT1', status=Order.STATUS_PAID,
            datetime=timezone.now(), expires=timezone.now() + datetime.timedelta(days=1),
            total=10, sales_channel=channel,
        )
        self.pos = OrderPosition.objects.create(
            order=self.order, item=self.item, price=10, tax_rate=0, tax_value=0, positionid=1,
        )
        QuestionAnswer.objects.create(orderposition=self.pos, question=self.question, answer='a 2')

    def fix(self, client):
        return client.post(self.control_url('audit/'), {'action': 'fix'})

    def assignment_exists(self):
        return SeatAssignment.objects.filter(event=self.event, seat_guid='a2', order_position_id=self.pos.id).exists()

    # -- who may look --------------------------------------------------------
    def test_settings_only_user_cannot_open_the_audit(self):
        client = self.admin_client('settings@example.org', [SETTINGS])
        resp = client.get(self.control_url('audit/'))
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(b'AUDIT1', resp.content)

    def test_orders_only_user_cannot_open_the_audit(self):
        client = self.admin_client('orders@example.org', [ORDERS_READ, ORDERS_WRITE])
        self.assertEqual(client.get(self.control_url('audit/')).status_code, 403)

    def test_settings_plus_order_read_can_view_the_audit(self):
        client = self.admin_client('viewer@example.org', [SETTINGS, ORDERS_READ])
        resp = client.get(self.control_url('audit/'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'AUDIT1', resp.content)

    # -- who may change ------------------------------------------------------
    def test_read_only_user_cannot_apply_the_fix(self):
        client = self.admin_client('viewer@example.org', [SETTINGS, ORDERS_READ])
        resp = self.fix(client)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.assignment_exists())

    def test_user_with_order_write_can_apply_the_fix(self):
        client = self.admin_client('editor@example.org', [SETTINGS, ORDERS_READ, ORDERS_WRITE])
        resp = self.fix(client)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertTrue(self.assignment_exists())

    def test_full_admin_can_do_everything(self):
        client = self.admin_client()
        self.assertEqual(client.get(self.control_url('audit/')).status_code, 200)
        self.assertEqual(self.fix(client).status_code, 200)
        self.assertTrue(self.assignment_exists())

    def test_plain_audit_get_never_modifies_anything(self):
        client = self.admin_client()
        client.get(self.control_url('audit/'))
        self.assertFalse(self.assignment_exists())

    # -- the settings page keeps its own, unchanged permission ---------------
    def test_settings_page_still_only_needs_the_settings_permission(self):
        client = self.admin_client('settings@example.org', [SETTINGS])
        self.assertEqual(client.get(self.control_url()).status_code, 200)

    def test_user_without_settings_permission_cannot_open_the_settings_page(self):
        client = self.admin_client('orders@example.org', [ORDERS_READ])
        self.assertEqual(client.get(self.control_url()).status_code, 403)

    def test_navigation_hides_the_audit_link_from_users_who_cannot_use_it(self):
        settings_only = self.admin_client('settings@example.org', [SETTINGS])
        html = settings_only.get(self.control_url()).content.decode()
        self.assertIn('/simpleseatingplan/"', html)
        self.assertNotIn('/simpleseatingplan/audit/', html)

        viewer = self.admin_client('viewer@example.org', [SETTINGS, ORDERS_READ])
        self.assertIn('/simpleseatingplan/audit/', viewer.get(self.control_url()).content.decode())
