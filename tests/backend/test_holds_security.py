"""
Security regression tests for the seat hold / release endpoints.

Finding being covered: holds used to be tied to nothing but a client-supplied
`cartpos_id`, so anybody with a session could (a) hold a seat on behalf of
somebody else's cart position -- which on_validate_order then trusted to
overwrite the victim's "Seat" answer --, (b) release other people's holds,
and (c) hoard every seat of the plan.
"""
import datetime

from django.utils import timezone
from pretix.base.models import QuestionAnswer
from pretix.base.services.orders import OrderError

from pretix_simpleseatingplan.models import SeatHold
from pretix_simpleseatingplan.signals import on_validate_order

from .base import SeatingTestCase


class HoldOwnershipTests(SeatingTestCase):
    # -- the legitimate flow must keep working ------------------------------
    def test_owner_can_hold_and_release(self):
        client, (pos,) = self.new_shopper()
        resp = self.hold(client, 'a1', pos)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['ok'])
        self.assertTrue(SeatHold.objects.filter(event=self.event, seat_guid='a1', cart_position_id=pos).exists())

        resp = self.release(client, 'a1', pos)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(SeatHold.objects.filter(event=self.event, seat_guid='a1').exists())

    def test_two_tickets_of_one_cart_can_each_hold_a_seat(self):
        client, (pos1, pos2) = self.new_shopper(positions=2)
        self.assertEqual(self.hold(client, 'a1', pos1).status_code, 200)
        self.assertEqual(self.hold(client, 'a2', pos2).status_code, 200)
        self.assertEqual(SeatHold.objects.filter(event=self.event).count(), 2)

    def test_changing_seat_moves_the_hold(self):
        client, (pos,) = self.new_shopper()
        self.assertEqual(self.hold(client, 'a1', pos).status_code, 200)
        self.assertEqual(self.hold(client, 'a2', pos).status_code, 200)
        self.assertEqual(
            list(SeatHold.objects.filter(event=self.event).values_list('seat_guid', flat=True)), ['a2'],
            'a cart position may only ever hold one seat at a time',
        )

    def test_reholding_own_seat_is_idempotent(self):
        client, (pos,) = self.new_shopper()
        self.assertEqual(self.hold(client, 'a1', pos).status_code, 200)
        self.assertEqual(self.hold(client, 'a1', pos).status_code, 200)
        self.assertEqual(SeatHold.objects.filter(event=self.event, seat_guid='a1').count(), 1)

    def test_sold_and_unknown_seats_are_still_rejected(self):
        from pretix_simpleseatingplan.models import SeatAssignment
        SeatAssignment.objects.create(event=self.event, seat_guid='a3', order_position_id=999)
        client, (pos,) = self.new_shopper()
        self.assertEqual(self.hold(client, 'a3', pos).status_code, 409)
        self.assertEqual(self.hold(client, 'nope', pos).status_code, 400)

    # -- attacks ------------------------------------------------------------
    def test_cannot_hold_on_behalf_of_someone_elses_cart_position(self):
        victim, (victim_pos,) = self.new_shopper()
        attacker, (attacker_pos,) = self.new_shopper()
        resp = self.hold(attacker, 'a1', victim_pos)
        self.assertIn(resp.status_code, (400, 403, 404), resp.content)
        self.assertFalse(SeatHold.objects.filter(event=self.event).exists())

    def test_cannot_hold_without_any_cart(self):
        visitor = self.bare_visitor()
        for pos_id in ('0', '1', '999999', ''):
            resp = visitor.post(self.url('hold/'), {'seat_guid': 'a1', 'cartpos_id': pos_id})
            self.assertIn(resp.status_code, (400, 403, 404), (pos_id, resp.content))
        self.assertFalse(SeatHold.objects.filter(event=self.event).exists())

    def test_cannot_hold_with_a_position_from_another_event(self):
        # Same shopper, but the cart position belongs to a different event.
        from pretix.base.models import CartPosition, Event, Item
        import datetime
        from django.utils import timezone
        other = Event.objects.create(
            organizer=self.organizer, name='Other', slug='other', live=True,
            date_from=timezone.now() + datetime.timedelta(days=5),
        )
        other_item = Item.objects.create(event=other, name='T', default_price=1, active=True)
        client, _ = self.new_shopper()
        foreign = CartPosition.objects.create(
            event=other, cart_id=list(client.session['carts'].keys())[0], item=other_item, price=1,
            expires=timezone.now() + datetime.timedelta(minutes=30),
        )
        resp = self.hold(client, 'a1', foreign.id)
        self.assertIn(resp.status_code, (400, 403, 404), resp.content)

    def test_cannot_hoard_seats_with_a_single_cart_position(self):
        client, (pos,) = self.new_shopper()
        for guid in ('a1', 'a2', 'a3'):
            self.hold(client, guid, pos)
        self.assertEqual(SeatHold.objects.filter(event=self.event).count(), 1)

    def test_cannot_steal_a_hold_from_another_shopper(self):
        victim, (victim_pos,) = self.new_shopper()
        attacker, (attacker_pos,) = self.new_shopper()
        self.assertEqual(self.hold(victim, 'a1', victim_pos).status_code, 200)
        resp = self.hold(attacker, 'a1', attacker_pos)
        self.assertEqual(resp.status_code, 409, resp.content)
        hold = SeatHold.objects.get(event=self.event, seat_guid='a1')
        self.assertEqual(hold.cart_position_id, victim_pos)

    def test_cannot_release_someone_elses_hold(self):
        victim, (victim_pos,) = self.new_shopper()
        attacker, _ = self.new_shopper()
        self.assertEqual(self.hold(victim, 'a1', victim_pos).status_code, 200)
        resp = self.release(attacker, 'a1', victim_pos)
        self.assertIn(resp.status_code, (400, 403, 404), resp.content)
        self.assertTrue(SeatHold.objects.filter(event=self.event, seat_guid='a1').exists())

    def test_expired_hold_of_someone_else_can_be_taken_over(self):
        from django.utils import timezone
        import datetime
        victim, (victim_pos,) = self.new_shopper()
        self.assertEqual(self.hold(victim, 'a1', victim_pos).status_code, 200)
        SeatHold.objects.filter(event=self.event).update(expires=timezone.now() - datetime.timedelta(minutes=1))
        other, (other_pos,) = self.new_shopper()
        self.assertEqual(self.hold(other, 'a1', other_pos).status_code, 200)

    def test_hold_of_a_vanished_cart_can_be_taken_over(self):
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=987654,  # cart position no longer exists
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        client, (pos,) = self.new_shopper()
        self.assertEqual(self.hold(client, 'a1', pos).status_code, 200)
        self.assertEqual(SeatHold.objects.get(event=self.event, seat_guid='a1').cart_position_id, pos)

    def test_forged_hold_cannot_rewrite_the_victims_seat_answer(self):
        """End-to-end version of the original exploit: the attacker tries to
        get the victim's order silently attached to a seat of their choosing."""
        victim, (victim_pos,) = self.new_shopper()
        attacker, _ = self.new_shopper()
        self.hold(attacker, 'a3', victim_pos)  # forged: victim's cart position id

        from pretix.base.models import CartPosition
        victim_cp = CartPosition.objects.get(pk=victim_pos)
        QuestionAnswer.objects.create(cartposition=victim_cp, question=self.question, answer='A-1')
        try:
            on_validate_order(self.event, positions=[victim_cp])
        except OrderError:
            pass  # rejecting is fine; silently rewriting is not
        answer = QuestionAnswer.objects.get(cartposition=victim_cp, question=self.question)
        self.assertEqual(answer.answer, 'A-1')
