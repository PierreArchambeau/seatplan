"""
Cached ticket PDFs must not outlive the facts they were drawn from.

pretix keeps every generated ticket (CachedTicket) and only drops it when the
order, the item or the ticket layout changes. It knows nothing about this
plugin's seat assignments or plan, so a ticket generated before the seat was
recorded, or before a new plan was uploaded, kept showing no plan (or the old
one) until somebody happened to touch the order. The plugin now asks pretix to
invalidate the affected tickets itself.
"""
import json

from pretix.base.models import CachedTicket, Order
from pretix.base.signals import order_modified, order_placed

from pretix_simpleseatingplan.audit import run_seat_audit
from pretix_simpleseatingplan.models import SeatAssignment

from .test_order_flow import OrderFlowTestCase
from .base import SVG


class TicketCacheInvalidationTests(OrderFlowTestCase):
    def cached(self, pos):
        return CachedTicket.objects.create(order_position=pos, provider='pdf', type='application/pdf', extension='.pdf')

    def has_cache(self, pos):
        return CachedTicket.objects.filter(order_position=pos).exists()

    def unassigned_paid_order(self, label):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.answer(pos, self.question, label)
        return order, pos

    # -- the audit repairs missing assignments ---------------------------------
    def test_fixing_a_missing_assignment_drops_that_orders_stale_ticket(self):
        order, pos = self.unassigned_paid_order('A-2')
        other_order, other_pos = self.unassigned_paid_order('A-3')
        SeatAssignment.objects.create(event=self.event, seat_guid='a3', order_position_id=other_pos.id)  # nothing to fix here
        self.cached(pos)
        self.cached(other_pos)
        with self.captureOnCommitCallbacks(execute=True):
            run_seat_audit(self.event, self.cfg, fix=True)
        self.assertTrue(self.assigned('a2', pos))
        self.assertFalse(self.has_cache(pos), 'the ticket drawn before the seat existed is stale')
        self.assertTrue(self.has_cache(other_pos), 'orders that were not repaired keep their cache')

    def test_a_dry_run_changes_nothing(self):
        order, pos = self.unassigned_paid_order('A-2')
        self.cached(pos)
        with self.captureOnCommitCallbacks(execute=True):
            run_seat_audit(self.event, self.cfg, fix=False)
        self.assertTrue(self.has_cache(pos))

    def test_a_conflict_that_is_not_repaired_keeps_its_cache(self):
        _o1, first = self.unassigned_paid_order('A-1')
        _o2, second = self.unassigned_paid_order('a 1')
        self.cached(first)
        self.cached(second)
        with self.captureOnCommitCallbacks(execute=True):
            run_seat_audit(self.event, self.cfg, fix=True)
        self.assertTrue(self.has_cache(first) and self.has_cache(second))

    # -- an edited seat answer is re-synced -------------------------------------
    def test_moving_a_ticket_to_another_seat_drops_its_stale_ticket(self):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.answer(pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order)
        self.cached(pos)
        self.answer(pos, self.question, 'A-3')
        with self.captureOnCommitCallbacks(execute=True):
            order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a3', pos))
        self.assertFalse(self.has_cache(pos))

    def test_an_edit_that_changes_nothing_keeps_the_cache(self):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.answer(pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order)
        self.cached(pos)
        with self.captureOnCommitCallbacks(execute=True):
            order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.has_cache(pos))

    # -- a new plan is uploaded --------------------------------------------------
    def test_uploading_a_new_plan_drops_every_cached_ticket_of_the_event(self):
        order_a, pos_a = self.order_with_position(status=Order.STATUS_PAID)
        order_b, pos_b = self.order_with_position(status=Order.STATUS_PAID)
        self.cached(pos_a)
        self.cached(pos_b)
        admin = self.admin_client()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.upload_plan(admin, svg=SVG.replace('#22c55e', '#3366ff'))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.has_cache(pos_a) or self.has_cache(pos_b))

    def test_a_rejected_upload_keeps_the_cache(self):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.cached(pos)
        admin = self.admin_client()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.upload_plan(admin, svg='<html>not a plan</html>')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.has_cache(pos))

    def test_saving_settings_without_a_new_plan_keeps_the_cache(self):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.cached(pos)
        admin = self.admin_client()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.upload_plan(admin, hold_minutes=15)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.has_cache(pos), 'nothing about the plan changed')
