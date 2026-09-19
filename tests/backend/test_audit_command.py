"""
Tests for the `simpleseating_audit` management command and the audit logic
it shares with the control-panel page (run_seat_audit).

The audit is the one code path that writes seat assignments in bulk for
orders that are already paid, so its refusals matter as much as its
repairs: it must never overwrite a sale, never "fix" an ambiguous claim, and
never touch orders it is not meant to look at.
"""
import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.utils import timezone
from pretix.base.models import Event, Item, Order, OrderPosition, Question, Quota
from pretix.base.signals import order_placed

from pretix_simpleseatingplan.audit import run_seat_audit
from pretix_simpleseatingplan.forms import Q_SEAT_LABEL
from pretix_simpleseatingplan.models import Seat, SeatAssignment, SeatingConfig

from .base import SVG, no_active_scope
from .test_order_flow import OrderFlowTestCase


class AuditLogicTests(OrderFlowTestCase):
    def missing(self, label, status=Order.STATUS_PAID):
        """A paid order whose seat was never recorded (the bug the audit repairs)."""
        order, pos = self.order_with_position(status=status)
        self.answer(pos, self.question, label)
        return order, pos

    def sold(self, label):
        """An order that went through order_placed normally."""
        order, pos = self.order_with_position()
        self.answer(pos, self.question, label)
        order_placed.send(sender=self.event, order=order)
        return order, pos

    # -- refusals: it must not overwrite or guess -----------------------------
    def test_claim_on_an_already_sold_seat_is_a_conflict_and_never_overwrites_the_sale(self):
        sold_order, sold_pos = self.sold('A-1')
        _, claimant = self.missing('a 1')

        result = run_seat_audit(self.event, self.cfg, fix=True)

        self.assertEqual(result['resolved'], [])
        (conflict,) = result['conflicts']
        self.assertEqual(conflict['seat_guid'], 'a1')
        self.assertEqual(conflict['already_sold_position_id'], sold_pos.id)
        self.assertEqual(conflict['already_sold_order'].id, sold_order.id)
        self.assertEqual([pos.id for _o, pos, _l in conflict['claims']], [claimant.id])
        self.assertEqual(
            list(SeatAssignment.objects.filter(event=self.event).values_list('order_position_id', flat=True)),
            [sold_pos.id], 'the existing sale must be left exactly as it was',
        )

    def test_conflict_with_an_assignment_whose_order_no_longer_exists_does_not_crash(self):
        SeatAssignment.objects.create(event=self.event, seat_guid='a1', order_position_id=987654)
        self.missing('A-1')
        result = run_seat_audit(self.event, self.cfg, fix=True)
        (conflict,) = result['conflicts']
        self.assertEqual(conflict['already_sold_position_id'], 987654)
        self.assertIsNone(conflict['already_sold_order'])
        self.assertIsNone(conflict['already_sold_position'])

    def test_two_claims_on_one_free_seat_are_never_resolved_automatically(self):
        self.missing('A-2')
        self.missing('a2')
        result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(len(result['conflicts'][0]['claims']), 2)
        self.assertEqual(result['resolved'], [])
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_unambiguous_positions_are_fixed_even_when_others_are_in_conflict(self):
        _, ok = self.missing('A-3')
        self.missing('A-2')
        self.missing('A-2')
        result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual([r['seat_guid'] for r in result['resolved']], ['a3'])
        self.assertTrue(self.assigned('a3', ok))
        self.assertEqual(SeatAssignment.objects.filter(event=self.event, seat_guid='a2').count(), 0)

    # -- scope: which positions it looks at ----------------------------------
    def test_orders_that_are_cancelled_or_expired_are_ignored(self):
        self.missing('A-1', status=Order.STATUS_CANCELED)
        self.missing('A-2', status=Order.STATUS_EXPIRED)
        result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual((result['resolved'], result['conflicts'], result['unmatched']), ([], [], []))
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_pending_orders_are_audited_too(self):
        _, pos = self.missing('A-1', status=Order.STATUS_PENDING)
        run_seat_audit(self.event, self.cfg, fix=True)
        self.assertTrue(self.assigned('a1', pos))

    def test_positions_of_other_items_are_ignored(self):
        other_item = Item.objects.create(event=self.event, name='Parking', default_price=5, active=True)
        order, _ = self.order_with_position(status=Order.STATUS_PAID)
        other_pos = OrderPosition.objects.create(
            order=order, item=other_item, price=5, tax_rate=0, tax_value=0, positionid=2,
        )
        self.answer(other_pos, self.question, 'A-1')
        result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertNotIn(other_pos.id, [r['position'].id for r in result['resolved']])
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_positions_that_already_have_an_assignment_are_skipped(self):
        _, pos = self.sold('A-1')
        result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual((result['resolved'], result['conflicts'], result['unmatched']), ([], [], []))
        self.assertEqual(SeatAssignment.objects.filter(event=self.event).count(), 1)

    def test_other_events_are_never_touched(self):
        other = Event.objects.create(
            organizer=self.organizer, name='Other', slug='other', live=True,
            date_from=timezone.now() + datetime.timedelta(days=9), plugins='pretix_simpleseatingplan',
        )
        other_item = Item.objects.create(event=other, name='T', default_price=1, active=True)
        other_q = Question.objects.create(event=other, identifier=Q_SEAT_LABEL, question={'en': 'Seat'}, type=Question.TYPE_STRING)
        Seat.objects.create(event=other, seat_guid='a1', label='A-1')
        other_cfg = SeatingConfig.objects.create(event=other, item_id=other_item.id, question_label_id=other_q.id, svg=SVG)
        self.missing('A-1')  # missing in *this* event only
        run_seat_audit(other, other_cfg, fix=True)
        self.assertFalse(SeatAssignment.objects.exists(), 'auditing another event must not assign this event\'s orders')

    # -- what it reports ----------------------------------------------------
    def test_labels_matching_no_seat_and_missing_answers_are_reported_as_unmatched(self):
        _, typo = self.missing('Z-99')
        order, blank = self.order_with_position(status=Order.STATUS_PAID)  # no answer at all
        result = run_seat_audit(self.event, self.cfg, fix=True)
        by_pos = {pos.id: label for _o, pos, label in result['unmatched']}
        self.assertEqual(by_pos, {typo.id: 'Z-99', blank.id: None})
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_dry_run_reports_but_writes_nothing(self):
        _, pos = self.missing('a 2')
        result = run_seat_audit(self.event, self.cfg, fix=False)
        self.assertEqual([(r['seat_guid'], r['fixed']) for r in result['resolved']], [('a2', False)])
        self.assertFalse(self.assigned('a2', pos))

    def test_fix_is_idempotent(self):
        _, pos = self.missing('a 2')
        first = run_seat_audit(self.event, self.cfg, fix=True)
        second = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual([r['fixed'] for r in first['resolved']], [True])
        self.assertEqual(second['resolved'], [])
        self.assertEqual(SeatAssignment.objects.filter(event=self.event).count(), 1)

    def test_a_seat_assigned_concurrently_is_reported_as_not_fixed(self):
        _, pos = self.missing('A-2')
        with mock.patch.object(SeatAssignment.objects, 'get_or_create', return_value=(mock.Mock(), False)):
            result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual([r['fixed'] for r in result['resolved']], [False])


class AuditCommandTests(OrderFlowTestCase):
    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        # No active django-scopes scope, exactly like `python -m pretix simpleseating_audit`.
        with no_active_scope():
            call_command('simpleseating_audit', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def missing(self, label, status=Order.STATUS_PAID):
        order, pos = self.order_with_position(status=status)
        self.answer(pos, self.question, label)
        return order, pos

    def test_runs_without_an_active_scope(self):
        """Regression: the command used to crash with ScopeError because pretix
        management commands must switch scoping off themselves."""
        out, _ = self.run_command()
        self.assertIn('Total:', out)

    def test_dry_run_lists_repairs_hints_at_fix_and_writes_nothing(self):
        order, pos = self.missing('a 2')
        out, _ = self.run_command()
        self.assertIn('== org/concert ==', out)
        self.assertIn('[MISSING]', out)
        self.assertIn(order.code, out)
        self.assertIn('would assign seat a2', out)
        self.assertIn('1 missing assignment(s) resolvable (0 fixed)', out)
        self.assertIn('Re-run with --fix', out)
        self.assertFalse(self.assigned('a2', pos))

    def test_fix_applies_the_repair_and_reports_it(self):
        order, pos = self.missing('a 2')
        out, _ = self.run_command('--fix')
        self.assertIn('[FIXED]', out)
        self.assertIn('(1 fixed)', out)
        self.assertNotIn('Re-run with --fix', out)
        self.assertTrue(self.assigned('a2', pos))

    def test_conflicts_are_listed_and_never_fixed(self):
        sold_order, sold_pos = self.order_with_position()
        self.answer(sold_pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=sold_order)
        order, claimant = self.missing('a1')

        out, _ = self.run_command('--fix')
        self.assertIn('[CONFLICT] seat a1', out)
        self.assertIn('already sold, position %d' % sold_pos.id, out)
        self.assertIn('order %s position %d (typed "a1")' % (order.code, claimant.id), out)
        self.assertIn('1 conflict(s) needing manual review', out)
        self.assertNotIn('[FIXED]', out)
        self.assertFalse(self.assigned('a1', claimant))
        self.assertTrue(self.assigned('a1', sold_pos))

    def test_unmatched_labels_are_listed(self):
        order, _ = self.missing('Z-99')
        out, _ = self.run_command()
        self.assertIn('[NO MATCH]', out)
        self.assertIn('"Z-99"', out)
        self.assertIn('1 unmatched label(s)', out)

    def test_a_concurrently_assigned_seat_is_reported_as_a_race_not_as_fixed(self):
        self.missing('A-2')
        with mock.patch.object(SeatAssignment.objects, 'get_or_create', return_value=(mock.Mock(), False)):
            out, _ = self.run_command('--fix')
        self.assertIn('[RACE] seat a2', out)
        self.assertIn('(0 fixed)', out)

    # -- --event filtering ---------------------------------------------------
    def make_second_event(self):
        other = Event.objects.create(
            organizer=self.organizer, name='Other', slug='other', live=True,
            date_from=timezone.now() + datetime.timedelta(days=9), plugins='pretix_simpleseatingplan',
        )
        item = Item.objects.create(event=other, name='T', default_price=1, active=True)
        q = Question.objects.create(event=other, identifier=Q_SEAT_LABEL, question={'en': 'Seat'}, type=Question.TYPE_STRING)
        SeatingConfig.objects.create(event=other, item_id=item.id, question_label_id=q.id, svg=SVG)
        Seat.objects.create(event=other, seat_guid='a1', label='A-1')
        channel = self.channel
        order = Order.objects.create(
            event=other, organizer=self.organizer, code='OTHER1', status=Order.STATUS_PAID,
            datetime=timezone.now(), expires=timezone.now() + datetime.timedelta(days=1), total=1, sales_channel=channel,
        )
        pos = OrderPosition.objects.create(order=order, item=item, price=1, tax_rate=0, tax_value=0, positionid=1)
        self.answer(pos, q, 'A-1')
        return other, pos

    def test_without_event_option_every_configured_event_is_audited(self):
        other, other_pos = self.make_second_event()
        _, pos = self.missing('A-1')
        out, _ = self.run_command('--fix')
        self.assertIn('== org/concert ==', out)
        self.assertIn('== org/other ==', out)
        self.assertTrue(self.assigned('a1', pos))
        self.assertTrue(SeatAssignment.objects.filter(event=other, order_position_id=other_pos.id).exists())

    def test_event_option_restricts_the_audit_to_that_event(self):
        other, other_pos = self.make_second_event()
        _, pos = self.missing('A-1')
        out, _ = self.run_command('--fix', '--event', 'org/other')
        self.assertIn('== org/other ==', out)
        self.assertNotIn('== org/concert ==', out)
        self.assertTrue(SeatAssignment.objects.filter(event=other, order_position_id=other_pos.id).exists())
        self.assertFalse(self.assigned('a1', pos), 'the event that was not selected must not be touched')

    def test_malformed_event_option_is_rejected_without_doing_anything(self):
        _, pos = self.missing('A-1')
        out, err = self.run_command('--fix', '--event', 'no-slash-here')
        self.assertIn('--event must be given as organizer/slug', err)
        self.assertNotIn('Total:', out)
        self.assertFalse(self.assigned('a1', pos))

    def test_unknown_event_audits_nothing(self):
        _, pos = self.missing('A-1')
        out, _ = self.run_command('--fix', '--event', 'org/does-not-exist')
        self.assertNotIn('==', out)
        self.assertIn('0 missing assignment(s)', out)
        self.assertFalse(self.assigned('a1', pos))

    def test_events_with_an_incomplete_configuration_are_skipped(self):
        self.missing('A-1')
        SeatingConfig.objects.filter(event=self.event).update(question_label_id=0)
        out, _ = self.run_command('--fix')
        self.assertNotIn('== org/concert ==', out)
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_events_without_any_seating_configuration_are_not_audited(self):
        SeatingConfig.objects.filter(event=self.event).delete()
        out, _ = self.run_command()
        self.assertNotIn('==', out)
