"""
Regression tests for the order lifecycle signals: validate_cart,
validate_order, order_placed, order_modified and the seat audit.

Ported from the ad-hoc script that used to live next to the repo
(test_seatplan.py, run inside `pretix shell` against the dev database) so
they run in isolation on a throwaway database.
"""
import datetime

from django.utils import timezone
from pretix.base.models import (
    CartPosition, Order, OrderPosition, Question, QuestionAnswer, SalesChannel,
)
from pretix.base.services.cart import CartError
from pretix.base.services.orders import OrderError
from pretix.base.signals import order_modified, order_placed, validate_cart, validate_order

from pretix_simpleseatingplan.audit import run_seat_audit
from pretix_simpleseatingplan.models import Seat, SeatAssignment, SeatHold

from .base import SeatingTestCase


class OrderFlowTestCase(SeatingTestCase):
    def setUp(self):
        super().setUp()
        self.q_name = Question.objects.create(
            event=self.event, question={'en': 'Full name'}, type=Question.TYPE_STRING, required=False,
        )
        self.q_name.items.add(self.item)
        self.channel = SalesChannel.objects.get(organizer=self.organizer, identifier='web')
        Seat.objects.create(event=self.event, seat_guid='a4', label='A-4', category='')
        self._order_seq = 0

    # -- builders -----------------------------------------------------------
    def cartpos(self, cart_id='test-cart'):
        return CartPosition.objects.create(
            event=self.event, cart_id=cart_id, item=self.item, price=10,
            expires=timezone.now() + datetime.timedelta(minutes=30),
        )

    def answer(self, position, question, value):
        QuestionAnswer.objects.update_or_create(
            cartposition=position if isinstance(position, CartPosition) else None,
            orderposition=position if isinstance(position, OrderPosition) else None,
            question=question, defaults={'answer': value},
        )

    def order_with_position(self, status=Order.STATUS_PENDING):
        self._order_seq += 1
        order = Order.objects.create(
            event=self.event, organizer=self.organizer, code='TEST%02d' % self._order_seq, status=status,
            datetime=timezone.now(), expires=timezone.now() + datetime.timedelta(days=1),
            total=10, sales_channel=self.channel,
        )
        pos = OrderPosition.objects.create(order=order, item=self.item, price=10, tax_rate=0, tax_value=0, positionid=1)
        return order, pos

    def assigned(self, guid, position):
        return SeatAssignment.objects.filter(event=self.event, seat_guid=guid, order_position_id=position.id).exists()


class ValidationTests(OrderFlowTestCase):
    def test_tolerant_matching_accepts_loosely_typed_label(self):
        cp = self.cartpos()
        self.answer(cp, self.question, 'a 1')
        validate_order.send(sender=self.event, positions=[cp])  # must not raise

    def test_seat_number_typed_into_the_name_field_is_rejected_at_cart_validation(self):
        cp = self.cartpos()
        self.answer(cp, self.q_name, 'A-2')
        self.answer(cp, self.question, '')
        with self.assertRaises(CartError):
            validate_cart.send(sender=self.event, positions=[cp])

    def test_unknown_seat_label_is_rejected(self):
        cp = self.cartpos()
        self.answer(cp, self.question, 'Z-99-does-not-exist')
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[cp])

    def test_hold_wins_over_stale_answer_text_and_resyncs_the_text(self):
        """The customer clicked a seat (hold) but the answer text is stale:
        validation trusts the hold and rewrites the text to the held seat's
        label, since order_placed can only re-derive the seat from the text."""
        cp = self.cartpos()
        SeatHold.objects.create(
            event=self.event, seat_guid='a4', cart_position_id=cp.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        self.answer(cp, self.question, 'Z-99-does-not-exist')
        validate_order.send(sender=self.event, positions=[cp])
        self.assertEqual(QuestionAnswer.objects.get(cartposition=cp, question=self.question).answer, 'A-4')

    def test_hold_based_order_ends_up_with_a_seat_assignment(self):
        cp = self.cartpos()
        SeatHold.objects.create(
            event=self.event, seat_guid='a4', cart_position_id=cp.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        self.answer(cp, self.question, 'stale')
        validate_order.send(sender=self.event, positions=[cp])
        synced = QuestionAnswer.objects.get(cartposition=cp, question=self.question).answer

        self.order_with_position()  # shift the OrderPosition id sequence away from the CartPosition one
        order, pos = self.order_with_position()
        self.assertNotEqual(pos.id, cp.id, 'OrderPosition ids are unrelated to CartPosition ids')
        self.answer(pos, self.question, synced)  # what transform_cart_positions copies over
        order_placed.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a4', pos))
        self.assertFalse(SeatHold.objects.filter(event=self.event, seat_guid='a4').exists(), 'hold is consumed')

    def test_sold_seat_is_rejected_for_a_new_order(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a1', pos))

        cp = self.cartpos('other-cart')
        self.answer(cp, self.question, 'A-1')
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[cp])

    def test_category_mismatch_is_rejected(self):
        Seat.objects.filter(event=self.event, seat_guid='a1').update(category='Cat I')
        self.cfg.category_variation_map = 'Cat I = 12345'
        self.cfg.save()
        from pretix.base.models import ItemVariation
        variation = ItemVariation.objects.create(item=self.item, value='Regular')
        cp = self.cartpos()
        cp.variation = variation
        cp.save()
        self.answer(cp, self.question, 'A-1')
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[cp])


class OrderPlacedAndModifiedTests(OrderFlowTestCase):
    def test_order_placed_assigns_the_tolerantly_matched_seat(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'a-02')
        order_placed.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a2', pos))

    def test_order_modified_moves_the_assignment_to_the_corrected_seat(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'a-02')
        order_placed.send(sender=self.event, order=order)
        self.answer(pos, self.question, 'A-3')
        order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a3', pos))
        self.assertFalse(SeatAssignment.objects.filter(event=self.event, seat_guid='a2').exists())

    def test_order_modified_never_steals_a_seat_sold_to_another_order(self):
        order1, pos1 = self.order_with_position()
        self.answer(pos1, self.question, 'a-02')
        order_placed.send(sender=self.event, order=order1)
        self.answer(pos1, self.question, 'A-3')
        order_modified.send(sender=self.event, order=order1)

        order2, pos2 = self.order_with_position()
        self.answer(pos2, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order2)

        self.answer(pos1, self.question, 'A-1')  # tries to grab order2's seat
        order_modified.send(sender=self.event, order=order1)
        self.assertTrue(self.assigned('a1', pos2))
        self.assertTrue(self.assigned('a3', pos1))

    def test_cancelling_an_order_frees_its_seat(self):
        from pretix.base.signals import order_canceled
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order)
        order_canceled.send(sender=self.event, order=order)
        self.assertFalse(SeatAssignment.objects.filter(event=self.event, seat_guid='a1').exists())


class AuditTests(OrderFlowTestCase):
    def test_audit_finds_and_fixes_a_missing_assignment(self):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        self.answer(pos, self.question, 'a 2')

        dry = run_seat_audit(self.event, self.cfg, fix=False)
        self.assertTrue(any(r['order'].id == order.id and r['seat_guid'] == 'a2' for r in dry['resolved']))
        self.assertFalse(self.assigned('a2', pos))

        run_seat_audit(self.event, self.cfg, fix=True)
        self.assertTrue(self.assigned('a2', pos))

    def test_audit_reports_two_orders_claiming_one_seat_as_a_conflict(self):
        _, pos1 = self.order_with_position(status=Order.STATUS_PAID)
        _, pos2 = self.order_with_position(status=Order.STATUS_PAID)
        self.answer(pos1, self.question, 'A-1')
        self.answer(pos2, self.question, 'a1')
        result = run_seat_audit(self.event, self.cfg, fix=True)
        self.assertEqual([c['seat_guid'] for c in result['conflicts']], ['a1'])
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists(), 'conflicts are never auto-resolved')


class ServerSideSeatExclusivityTests(OrderFlowTestCase):
    """Holds used to be purely advisory: the server only refused seats that
    were already *sold*, so a seat somebody else was in the middle of buying
    could be taken by typing its label, and two buyers typing the same free
    seat at the same time both passed validation (validate_order runs before
    pretix takes its order lock, so nothing else serialises them)."""

    def test_seat_held_by_another_cart_is_refused_when_typed_by_hand(self):
        owner = self.cartpos('cart-owner')
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=owner.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        intruder = self.cartpos('cart-intruder')
        self.answer(intruder, self.question, 'A-1')
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[intruder])
        self.assertEqual(SeatHold.objects.get(event=self.event, seat_guid='a1').cart_position_id, owner.id)

    def test_owner_of_the_hold_can_still_check_out_with_that_seat(self):
        owner = self.cartpos('cart-owner')
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=owner.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        self.answer(owner, self.question, 'A-1')
        validate_order.send(sender=self.event, positions=[owner])

    def test_expired_hold_of_another_cart_does_not_block(self):
        owner = self.cartpos('cart-owner')
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=owner.id,
            expires=timezone.now() - datetime.timedelta(minutes=1),
        )
        other = self.cartpos('cart-other')
        self.answer(other, self.question, 'A-1')
        validate_order.send(sender=self.event, positions=[other])

    def test_two_buyers_typing_the_same_free_seat_cannot_both_validate(self):
        first = self.cartpos('cart-1')
        second = self.cartpos('cart-2')
        self.answer(first, self.question, 'A-2')
        self.answer(second, self.question, 'a 2')
        validate_order.send(sender=self.event, positions=[first])  # claims the seat
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[second])

    def test_validating_again_after_a_failed_attempt_still_works_for_the_claimant(self):
        cp = self.cartpos()
        self.answer(cp, self.question, 'A-2')
        validate_order.send(sender=self.event, positions=[cp])
        validate_order.send(sender=self.event, positions=[cp])  # retry of the same checkout

    def test_claim_is_released_once_the_order_is_placed(self):
        cp = self.cartpos()
        self.answer(cp, self.question, 'A-2')
        validate_order.send(sender=self.event, positions=[cp])
        self.assertEqual(SeatHold.objects.filter(event=self.event, seat_guid='a2').count(), 1)
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-2')
        order_placed.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a2', pos))
        self.assertFalse(SeatHold.objects.filter(event=self.event, seat_guid='a2').exists())

    def test_a_held_seat_that_was_sold_in_the_meantime_is_not_reassigned(self):
        buyer_order, buyer_pos = self.order_with_position()
        self.answer(buyer_pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=buyer_order)

        stale = self.cartpos('cart-stale')
        SeatHold.objects.create(  # a hold that survived (e.g. created just before the sale)
            event=self.event, seat_guid='a1', cart_position_id=stale.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        self.answer(stale, self.question, 'A-1')
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[stale])

    def test_orphaned_hold_does_not_lock_a_seat_out(self):
        """A hold whose cart position is gone (cart expired/deleted) protects
        nobody: the customer who retries with a fresh cart must not be
        refused for the rest of the hold timer."""
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=987654,  # no such cart position
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        cp = self.cartpos()
        self.answer(cp, self.question, 'A-1')
        validate_order.send(sender=self.event, positions=[cp])
        self.assertEqual(SeatHold.objects.get(event=self.event, seat_guid='a1').cart_position_id, cp.id)
