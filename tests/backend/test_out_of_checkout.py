"""
Seats sold outside the seat picker's checkout flow.

validate_order only runs for orders placed through the shop checkout. Orders
created through the REST API, the order import, or edited afterwards by staff
or by the customer never pass through it, so the plugin has two lines of
defence there:

* Where the order is still being created inside a database transaction (the
  shop checkout), order_placed refuses to sell a seat that already belongs to
  another position: raising rolls the whole order back, so nothing is sold twice.
* Where the order has already been committed (REST API, import) or was
  edited after the fact, rejecting is no longer possible -- raising would leave
  a committed order behind an error response. The conflict is recorded in the
  order's history instead, so staff can see it without reading server logs, and
  the sale that was already recorded is never overwritten.
"""
import datetime
import json
from unittest import mock

from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone
from pretix.base.models import CartPosition, Item, Order, OrderPosition, Team
from pretix.base.services.orders import OrderError, _perform_order
from pretix.base.signals import order_modified, order_placed, validate_order

from pretix_simpleseatingplan.audit import run_seat_audit
from pretix_simpleseatingplan.models import SeatAssignment, SeatHold
from pretix_simpleseatingplan.signals import _order_can_still_be_rolled_back

from .base import SeatingTransactionTestCase
from .test_order_flow import OrderFlowTestCase

CONFLICT = 'pretix_simpleseatingplan.seat_conflict'
EDIT_IGNORED = 'pretix_simpleseatingplan.seat_edit_ignored'


def entries(order, action):
    return [e for e in order.all_logentries().filter(action_type=action)]


class TransactionDetectionTests(TransactionTestCase):
    """The rest of this module relies on telling apart 'the order can still be
    rolled back' from 'the order is already committed'."""

    def test_false_outside_a_transaction_true_inside_one(self):
        self.assertFalse(_order_can_still_be_rolled_back())
        with transaction.atomic():
            self.assertTrue(_order_can_still_be_rolled_back())
        self.assertFalse(_order_can_still_be_rolled_back())


class CheckoutFailClosedTests(OrderFlowTestCase):
    """Runs the real pretix order creation (`_perform_order`), signals and all."""

    def setUp(self):
        super().setUp()
        # Free tickets: no payment provider is needed to complete such an order.
        Item.objects.filter(pk=self.item.pk).update(default_price=0)

    def place(self, *positions):
        """Runs the real order creation; returns the created Order."""
        result = self._perform(*positions)
        return Order.objects.get(pk=result['order_id']), result['warnings']

    def _perform(self, *positions):
        return _perform_order(
            self.event, payment_requests=[], position_ids=[p.id for p in positions],
            email='buyer@example.org', locale='en', address=None, meta_info={},
            sales_channel='web', shown_total=None,
        )

    def shopper_position(self, label=None):
        _client, (pos_id,) = self.new_shopper()
        cp = CartPosition.objects.get(pk=pos_id)
        if label is not None:
            self.answer(cp, self.question, label)
        return cp

    def test_normal_checkout_still_creates_the_order_and_the_assignment(self):
        cp = self.shopper_position('a 2')
        order, _warnings = self.place(cp)
        (pos,) = order.positions.all()
        self.assertTrue(self.assigned('a2', pos))
        self.assertEqual(json.loads(pos.meta_info)['seat_number'], 'A-2')
        self.assertFalse(SeatHold.objects.filter(event=self.event, seat_guid='a2').exists(), 'the claim is consumed')

    def test_checkout_through_a_picker_hold_creates_the_assignment(self):
        client, (pos_id,) = self.new_shopper()
        self.assertEqual(self.hold(client, 'a3', pos_id).status_code, 200)
        cp = CartPosition.objects.get(pk=pos_id)
        self.answer(cp, self.question, 'garbage the customer typed afterwards')
        order, _ = self.place(cp)
        (pos,) = order.positions.all()
        self.assertTrue(self.assigned('a3', pos), 'the hold, not the stale text, decides')

    def test_two_tickets_for_the_same_seat_in_one_checkout_are_refused(self):
        client, (first, second) = self.new_shopper(positions=2)
        for pos_id in (first, second):
            self.answer(CartPosition.objects.get(pk=pos_id), self.question, 'A-1')
        with self.assertRaises(OrderError):
            self.place(CartPosition.objects.get(pk=first), CartPosition.objects.get(pk=second))
        self.assertFalse(Order.objects.filter(event=self.event).exists())

    def test_a_seat_sold_after_validation_refuses_the_order_and_rolls_everything_back(self):
        cp = self.shopper_position('A-1')
        # Somebody else's order (API, import, another checkout) takes A-1 *after*
        # this checkout passed validate_order. Emulate "validation already passed"
        # by not letting the validation receiver see the sale.
        other_order, other_pos = self.order_with_position()
        SeatAssignment.objects.create(event=self.event, seat_guid='a1', order_position_id=other_pos.id)
        validate_order.disconnect(dispatch_uid='simpleseating_validate_order')
        try:
            with self.assertRaises(OrderError) as ctx:
                self.place(cp)
        finally:
            from pretix_simpleseatingplan import signals
            validate_order.connect(signals.on_validate_order, dispatch_uid='simpleseating_validate_order')
        self.assertIn('A-1', str(ctx.exception))
        self.assertEqual(Order.objects.filter(event=self.event).count(), 1, 'only the other order exists')
        self.assertTrue(CartPosition.objects.filter(pk=cp.pk).exists(), 'the shopper keeps their cart')
        self.assertEqual(
            list(SeatAssignment.objects.filter(event=self.event).values_list('order_position_id', flat=True)),
            [other_pos.id],
        )

    def test_the_checkout_message_names_the_seat_so_the_shopper_can_pick_another(self):
        cp = self.shopper_position('A-2')
        other_order, other_pos = self.order_with_position()
        SeatAssignment.objects.create(event=self.event, seat_guid='a2', order_position_id=other_pos.id)
        validate_order.disconnect(dispatch_uid='simpleseating_validate_order')
        try:
            with self.assertRaises(OrderError) as ctx:
                self.place(cp)
        finally:
            from pretix_simpleseatingplan import signals
            validate_order.connect(signals.on_validate_order, dispatch_uid='simpleseating_validate_order')
        self.assertIn('A-2', str(ctx.exception))
        self.assertIn('choose another seat', str(ctx.exception))


class AlreadyCommittedOrderTests(OrderFlowTestCase):
    """order_placed for an order that can no longer be rolled back (REST API,
    import): never raise, never overwrite the earlier sale, record the conflict."""

    def committed(self):
        return mock.patch('pretix_simpleseatingplan.signals._order_can_still_be_rolled_back', return_value=False)

    def sold_to_someone(self, label='A-1'):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, label)
        order_placed.send(sender=self.event, order=order)
        return order, pos

    def test_conflict_is_recorded_in_the_order_history_and_the_sale_is_kept(self):
        first_order, first_pos = self.sold_to_someone('A-1')
        late_order, late_pos = self.order_with_position()
        self.answer(late_pos, self.question, 'a 1')
        with self.committed():
            with self.assertLogs('pretix_simpleseatingplan.signals', level='ERROR'):
                order_placed.send(sender=self.event, order=late_order)  # must not raise

        self.assertTrue(self.assigned('a1', first_pos))
        self.assertFalse(self.assigned('a1', late_pos))
        self.assertEqual(SeatAssignment.objects.filter(event=self.event).count(), 1)
        (entry,) = entries(late_order, CONFLICT)
        data = entry.parsed_data
        self.assertEqual(data['seat'], 'A-1')
        self.assertEqual(data['typed'], 'a 1')
        self.assertEqual(data['position'], late_pos.positionid)
        self.assertEqual(data['other_order'], first_order.code)

    def test_the_conflict_then_shows_up_in_the_seat_audit(self):
        first_order, first_pos = self.sold_to_someone('A-1')
        late_order, late_pos = self.order_with_position()
        late_order.status = Order.STATUS_PAID
        late_order.save()
        self.answer(late_pos, self.question, 'A-1')
        with self.committed():
            order_placed.send(sender=self.event, order=late_order)
        result = run_seat_audit(self.event, self.cfg, fix=True)
        (conflict,) = result['conflicts']
        self.assertEqual(conflict['already_sold_position_id'], first_pos.id)
        self.assertEqual([pos.id for _o, pos, _l in conflict['claims']], [late_pos.id])

    def test_a_non_conflicting_order_leaves_no_conflict_entry(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-2')
        with self.committed():
            order_placed.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a2', pos))
        self.assertEqual(entries(order, CONFLICT), [])

    def test_sending_order_placed_twice_is_harmless(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-2')
        order_placed.send(sender=self.event, order=order)
        order_placed.send(sender=self.event, order=order)  # not a conflict with itself
        self.assertEqual(SeatAssignment.objects.filter(event=self.event).count(), 1)
        self.assertEqual(entries(order, CONFLICT), [])

    def test_inside_a_transaction_the_same_conflict_refuses_the_order(self):
        first_order, first_pos = self.sold_to_someone('A-1')
        late_order, late_pos = self.order_with_position()
        self.answer(late_pos, self.question, 'A-1')
        with self.assertRaises(OrderError):
            order_placed.send(sender=self.event, order=late_order)  # test cases run inside a transaction
        self.assertTrue(self.assigned('a1', first_pos))


class EditedAfterwardsTests(OrderFlowTestCase):
    """Staff (or the customer, if the shop allows it) change the seat answer of
    an existing order. That cannot be refused, but it must not be silent."""

    def setUp(self):
        super().setUp()
        self.order_a, self.pos_a = self.order_with_position()
        self.answer(self.pos_a, self.question, 'A-1')
        order_placed.send(sender=self.event, order=self.order_a)
        self.order_b, self.pos_b = self.order_with_position()
        self.answer(self.pos_b, self.question, 'A-2')
        order_placed.send(sender=self.event, order=self.order_b)

    def modify(self, order):
        order_modified.send(sender=self.event, order=order)

    def test_editing_onto_a_seat_sold_to_another_order_is_recorded_and_changes_nothing(self):
        self.answer(self.pos_b, self.question, 'a 1')
        self.modify(self.order_b)
        self.assertTrue(self.assigned('a1', self.pos_a))
        self.assertTrue(self.assigned('a2', self.pos_b), 'the buyer keeps the seat they actually paid for')
        (entry,) = entries(self.order_b, EDIT_IGNORED)
        self.assertEqual(entry.parsed_data['reason'], 'seat_taken')
        self.assertEqual(entry.parsed_data['typed'], 'a 1')
        self.assertEqual(entry.parsed_data['other_order'], self.order_a.code)
        self.assertEqual(entry.parsed_data['kept_seat'], 'A-2')

    def test_editing_to_a_label_that_matches_no_seat_is_recorded(self):
        self.answer(self.pos_b, self.question, 'Z-99')
        self.modify(self.order_b)
        self.assertTrue(self.assigned('a2', self.pos_b))
        (entry,) = entries(self.order_b, EDIT_IGNORED)
        self.assertEqual(entry.parsed_data['reason'], 'unknown_seat')
        self.assertEqual(entry.parsed_data['typed'], 'Z-99')

    def test_the_same_bad_edit_is_not_recorded_over_and_over(self):
        self.answer(self.pos_b, self.question, 'Z-99')
        for _ in range(3):
            self.modify(self.order_b)
        self.assertEqual(len(entries(self.order_b, EDIT_IGNORED)), 1)

    def test_a_different_bad_edit_is_recorded_again(self):
        self.answer(self.pos_b, self.question, 'Z-99')
        self.modify(self.order_b)
        self.answer(self.pos_b, self.question, 'Y-88')
        self.modify(self.order_b)
        self.assertEqual(len(entries(self.order_b, EDIT_IGNORED)), 2)

    def test_a_legitimate_move_to_a_free_seat_is_not_flagged(self):
        self.answer(self.pos_b, self.question, 'A-3')
        self.modify(self.order_b)
        self.assertTrue(self.assigned('a3', self.pos_b))
        self.assertEqual(entries(self.order_b, EDIT_IGNORED), [])

    def test_untouched_orders_get_no_entries(self):
        self.modify(self.order_a)
        self.modify(self.order_b)
        self.assertEqual(entries(self.order_a, EDIT_IGNORED) + entries(self.order_b, EDIT_IGNORED), [])


class HistoryDisplayTests(OrderFlowTestCase):
    """LogEntry.display() is what the order history page calls."""

    def test_conflict_entries_read_as_a_sentence_naming_seat_and_other_order(self):
        first_order, first_pos = self.order_with_position()
        self.answer(first_pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=first_order)
        late_order, late_pos = self.order_with_position()
        self.answer(late_pos, self.question, 'A-1')
        with mock.patch('pretix_simpleseatingplan.signals._order_can_still_be_rolled_back', return_value=False):
            order_placed.send(sender=self.event, order=late_order)
        (entry,) = entries(late_order, CONFLICT)
        text = str(entry.display())
        self.assertIn('A-1', text)
        self.assertIn(first_order.code, text)
        self.assertIn('audit', text.lower())

    def test_edit_entries_read_as_a_sentence_for_every_reason(self):
        order_a, pos_a = self.order_with_position()
        self.answer(pos_a, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order_a)
        order_b, pos_b = self.order_with_position()
        self.answer(pos_b, self.question, 'A-2')
        order_placed.send(sender=self.event, order=order_b)

        for typed, reason_text in (('Z-99', 'no seat has that name'), ('A-1', 'already sold to order %s' % order_a.code), ('', 'left empty')):
            self.answer(pos_b, self.question, typed)
            order_modified.send(sender=self.event, order=order_b)
        texts = [str(e.display()) for e in entries(order_b, EDIT_IGNORED)]
        self.assertEqual(len(texts), 3)
        joined = ' | '.join(texts)
        for expected in ('Z-99', 'no seat has that name', 'already sold to order %s' % order_a.code, 'left empty', 'keeps seat A-2'):
            self.assertIn(expected, joined)

    def test_other_plugins_entries_are_left_alone(self):
        order, _pos = self.order_with_position()
        other = order.log_action('some_other.plugin.action', data={})
        self.assertEqual(other.display(), 'some_other.plugin.action')

    def test_entries_are_registered_as_order_entries_so_the_history_links_the_order(self):
        from pretix.base.logentrytypes import log_entry_types
        for action in (CONFLICT, EDIT_IGNORED):
            entry_type, _meta = log_entry_types.get(action_type=action)
            self.assertIsNotNone(entry_type, action)


class RestApiOrderTests(SeatingTransactionTestCase):
    """The real REST API, with real commits: order_placed runs after the order
    was committed, so the plugin cannot refuse it and must not blow up."""

    def setUp(self):
        super().setUp()
        team = Team.objects.create(
            organizer=self.organizer, name='API', all_events=True, all_event_permissions=True,
        )
        self.token = team.tokens.create(name='test').token

    def create_order(self, label):
        from django.test import Client
        payload = {
            'email': 'api@example.org', 'locale': 'en', 'sales_channel': 'web',
            'positions': [{
                'item': self.item.id,
                'answers': [{'question': self.question.id, 'answer': label, 'options': []}],
            }],
        }
        return Client().post(
            '/api/v1/organizers/org/events/concert/orders/', data=json.dumps(payload),
            content_type='application/json', HTTP_AUTHORIZATION='Token ' + self.token,
        )

    def position(self, response):
        return OrderPosition.objects.get(order__code=response.json()['code'])

    def test_api_order_for_a_free_seat_gets_its_assignment_and_no_conflict_entry(self):
        resp = self.create_order('a 3')
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        pos = self.position(resp)
        self.assertTrue(SeatAssignment.objects.filter(event=self.event, seat_guid='a3', order_position_id=pos.id).exists())
        self.assertEqual(entries(pos.order, CONFLICT), [])

    def test_api_order_for_a_sold_seat_is_created_not_crashed_and_the_conflict_is_recorded(self):
        first = self.create_order('A-1')
        self.assertEqual(first.status_code, 201, first.content[:400])
        first_pos = self.position(first)

        second = self.create_order('a 1')
        # The API commits the order *before* sending order_placed, so refusing it
        # is impossible. The client must get a normal answer, not a 500 with a
        # committed order behind it.
        self.assertEqual(second.status_code, 201, second.content[:400])
        second_pos = self.position(second)
        self.assertNotEqual(first_pos.order.code, second_pos.order.code)

        self.assertEqual(
            list(SeatAssignment.objects.filter(event=self.event).values_list('order_position_id', flat=True)),
            [first_pos.id], 'the first sale keeps the seat',
        )
        (entry,) = entries(second_pos.order, CONFLICT)
        self.assertEqual(entry.parsed_data['other_order'], first_pos.order.code)
        self.assertEqual(entry.parsed_data['seat'], 'A-1')

    def test_the_recorded_conflict_is_visible_in_the_orders_history_page(self):
        first = self.create_order('A-1')
        second = self.create_order('A-1')
        code = second.json()['code']
        admin = self.admin_client()
        resp = admin.get('/control/event/org/concert/orders/%s/' % code)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('Seat conflict', html)
        self.assertIn(first.json()['code'], html)
