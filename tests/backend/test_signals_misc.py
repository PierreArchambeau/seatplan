"""
Tests for the remaining pretix signal receivers: navigation, presale <head>
injection, category mapping, expiry, and
the branches of order_placed / order_modified that do not involve conflicts.
"""
import datetime
import json
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone
from pretix.base.models import (
    CartPosition, Item, ItemVariation, Order, OrderPosition, Question,
)
from pretix.base.services.cart import CartError
from pretix.base.services.orders import OrderError
from pretix.base.signals import (
    order_canceled, order_expired, order_modified, order_placed, validate_cart,
    validate_order,
)

from pretix_simpleseatingplan.models import Seat, SeatAssignment, SeatHold, SeatingConfig
from pretix_simpleseatingplan.signals import (
    _parse_category_map, inject_presale_head, nav_settings,
)

from .test_order_flow import OrderFlowTestCase


class NavigationTests(OrderFlowTestCase):
    def request_for(self, allowed, path='/control/event/org/concert/simpleseatingplan/'):
        user = mock.Mock()
        user.has_event_permission.side_effect = lambda org, ev, perm, **kw: perm in allowed
        return SimpleNamespace(user=user, organizer=self.organizer, event=self.event, path_info=path)

    def test_no_entries_without_the_settings_permission(self):
        self.assertEqual(nav_settings(None, self.request_for(set())), [])
        self.assertEqual(nav_settings(None, self.request_for({'event.orders:read'})), [])

    def test_settings_entry_only_for_settings_managers(self):
        (entry,) = nav_settings(None, self.request_for({'event.settings.general:write'}))
        self.assertTrue(entry['url'].endswith('/simpleseatingplan/'))
        self.assertTrue(entry['active'])

    def test_audit_entry_needs_the_right_to_view_orders_too_and_marks_itself_active(self):
        entries = nav_settings(None, self.request_for(
            {'event.settings.general:write', 'event.orders:read'}, path='/control/event/org/concert/simpleseatingplan/audit/'))
        self.assertEqual(len(entries), 2)
        self.assertFalse(entries[0]['active'])
        self.assertTrue(entries[1]['url'].endswith('/simpleseatingplan/audit/'))
        self.assertTrue(entries[1]['active'])


class PresaleHeadTests(OrderFlowTestCase):
    def test_injects_stylesheet_and_config_script_for_a_complete_configuration(self):
        html = inject_presale_head(self.event)
        self.assertIn('seatpicker.css', html)
        self.assertIn('_simpleseatingplan/config.js', html)

    def test_injects_nothing_when_incomplete(self):
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg='')
        self.assertEqual(inject_presale_head(self.event), '')
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg='<svg/>', question_label_id=0)
        self.assertEqual(inject_presale_head(self.event), '')

    def test_injects_nothing_when_the_seat_question_was_deleted(self):
        Question.objects.filter(pk=self.question.pk).delete()
        self.assertEqual(inject_presale_head(self.event), '')

    def test_injects_nothing_for_an_unconfigured_event(self):
        SeatingConfig.objects.all().delete()
        self.assertEqual(inject_presale_head(self.event), '')


class CategoryMapTests(OrderFlowTestCase):
    def test_parse_category_map(self):
        text = '# comment\n\nCat I = 3\n  Cat II=4  \nbroken line\nCat III = notanumber\nCat IV = 5 = 6\n'
        self.assertEqual(_parse_category_map(text), {'Cat I': 3, 'Cat II': 4})
        self.assertEqual(_parse_category_map(''), {})
        self.assertEqual(_parse_category_map(None), {})

    def make(self, seat_category='Cat I', mapped='Cat I'):
        Seat.objects.filter(event=self.event, seat_guid='a1').update(category=seat_category)
        good = ItemVariation.objects.create(item=self.item, value='Good')
        bad = ItemVariation.objects.create(item=self.item, value='Bad')
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(category_variation_map='%s = %d' % (mapped, good.pk))
        return good, bad

    def cart_with_hold(self, variation):
        cp = self.cartpos()
        cp.variation = variation
        cp.save()
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=cp.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        return CartPosition.objects.get(pk=cp.pk)

    def test_cart_rejects_a_ticket_type_that_does_not_match_the_held_seats_category(self):
        good, bad = self.make()
        with self.assertRaises(CartError):
            validate_cart.send(sender=self.event, positions=[self.cart_with_hold(bad)])

    def test_cart_accepts_the_matching_ticket_type(self):
        good, bad = self.make()
        validate_cart.send(sender=self.event, positions=[self.cart_with_hold(good)])

    def test_cart_ignores_seats_whose_category_is_not_mapped(self):
        good, bad = self.make(seat_category='Unmapped')
        validate_cart.send(sender=self.event, positions=[self.cart_with_hold(bad)])

    def test_cart_without_any_mapping_accepts_everything(self):
        good, bad = self.make()
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(category_variation_map='')
        validate_cart.send(sender=self.event, positions=[self.cart_with_hold(bad)])

    def test_order_accepts_the_matching_ticket_type(self):
        good, bad = self.make()
        cp = self.cart_with_hold(good)
        self.answer(cp, self.question, 'A-1')
        validate_order.send(sender=self.event, positions=[cp])


class ValidationSkipTests(OrderFlowTestCase):
    def test_events_without_configuration_or_item_are_not_validated(self):
        cp = self.cartpos()
        self.answer(cp, self.question, 'Z-99-nonsense')
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(item_id=0)
        validate_order.send(sender=self.event, positions=[cp])
        validate_cart.send(sender=self.event, positions=[cp])
        SeatingConfig.objects.all().delete()
        validate_order.send(sender=self.event, positions=[cp])
        validate_cart.send(sender=self.event, positions=[cp])

    def test_positions_of_other_items_are_not_validated(self):
        other = Item.objects.create(event=self.event, name='Parking', default_price=1, active=True)
        cp = CartPosition.objects.create(
            event=self.event, cart_id='c', item=other, price=1,
            expires=timezone.now() + datetime.timedelta(minutes=30),
        )
        self.answer(cp, self.question, 'Z-99-nonsense')
        validate_order.send(sender=self.event, positions=[cp])
        validate_cart.send(sender=self.event, positions=[cp])

    def test_without_a_seat_question_only_a_hold_can_supply_the_seat(self):
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(question_label_id=0)
        cp = self.cartpos()
        with self.assertRaises(OrderError):
            validate_order.send(sender=self.event, positions=[cp])
        SeatHold.objects.create(
            event=self.event, seat_guid='a2', cart_position_id=cp.id,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        validate_order.send(sender=self.event, positions=[cp])


class OrderPlacedBranchesTests(OrderFlowTestCase):
    def test_records_the_seat_in_meta_info_keeping_existing_keys(self):
        order, pos = self.order_with_position()
        OrderPosition.objects.filter(pk=pos.pk).update(meta_info=json.dumps({'keep': 'me'}))
        self.answer(pos, self.question, 'a 2')
        order_placed.send(sender=self.event, order=order)
        meta = json.loads(OrderPosition.objects.get(pk=pos.pk).meta_info)
        self.assertEqual(meta, {'keep': 'me', 'seat_number': 'A-2'})

    def test_recovers_from_broken_meta_info(self):
        order, pos = self.order_with_position()
        OrderPosition.objects.filter(pk=pos.pk).update(meta_info='{broken')
        self.answer(pos, self.question, 'A-2')
        order_placed.send(sender=self.event, order=order)
        self.assertEqual(json.loads(OrderPosition.objects.get(pk=pos.pk).meta_info), {'seat_number': 'A-2'})
        self.assertTrue(self.assigned('a2', pos))

    def test_a_position_without_a_resolvable_seat_gets_no_assignment_but_does_not_break_the_order(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'Z-99')
        with self.assertLogs('pretix_simpleseatingplan.signals', level='ERROR'):
            order_placed.send(sender=self.event, order=order)
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_ignores_other_items_and_unconfigured_events(self):
        other = Item.objects.create(event=self.event, name='Parking', default_price=1, active=True)
        order, _ = self.order_with_position()
        pos = OrderPosition.objects.create(order=order, item=other, price=1, tax_rate=0, tax_value=0, positionid=2)
        self.answer(pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order)
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())
        SeatingConfig.objects.all().delete()
        order_placed.send(sender=self.event, order=order)  # must simply return

    def test_leftover_holds_for_the_sold_seat_are_cleaned_up(self):
        SeatHold.objects.create(
            event=self.event, seat_guid='a2', cart_position_id=999,
            expires=timezone.now() + datetime.timedelta(minutes=10),
        )
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-2')
        order_placed.send(sender=self.event, order=order)
        self.assertFalse(SeatHold.objects.filter(event=self.event, seat_guid='a2').exists())


class OrderModifiedBranchesTests(OrderFlowTestCase):
    def placed(self, label):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, label)
        order_placed.send(sender=self.event, order=order)
        return order, pos

    def test_unchanged_answer_is_a_no_op(self):
        order, pos = self.placed('A-1')
        order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a1', pos))
        self.assertEqual(SeatAssignment.objects.filter(event=self.event).count(), 1)

    def test_typing_a_seat_that_matches_nothing_keeps_the_previous_assignment(self):
        order, pos = self.placed('A-1')
        self.answer(pos, self.question, 'Z-99')
        with self.assertLogs('pretix_simpleseatingplan.signals', level='WARNING'):
            order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a1', pos))

    def test_clearing_the_answer_keeps_the_previous_assignment(self):
        order, pos = self.placed('A-1')
        self.answer(pos, self.question, '')
        order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a1', pos))

    def test_a_position_without_assignment_gets_one_when_its_answer_is_edited_in(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'a 3')
        order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a3', pos))
        self.assertEqual(json.loads(OrderPosition.objects.get(pk=pos.pk).meta_info)['seat_number'], 'A-3')

    def test_moving_to_a_new_seat_updates_meta_info_and_frees_the_old_one(self):
        order, pos = self.placed('A-1')
        self.answer(pos, self.question, 'A-3')
        order_modified.send(sender=self.event, order=order)
        self.assertEqual(json.loads(OrderPosition.objects.get(pk=pos.pk).meta_info)['seat_number'], 'A-3')
        self.assertFalse(SeatAssignment.objects.filter(event=self.event, seat_guid='a1').exists())

    def test_incomplete_configuration_is_ignored(self):
        order, pos = self.placed('A-1')
        self.answer(pos, self.question, 'A-3')
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(question_label_id=0)
        order_modified.send(sender=self.event, order=order)
        self.assertTrue(self.assigned('a1', pos))
        SeatingConfig.objects.all().delete()
        order_modified.send(sender=self.event, order=order)


class ExpiryTests(OrderFlowTestCase):
    def test_expiring_an_order_frees_its_seats(self):
        order, pos = self.order_with_position()
        self.answer(pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=order)
        order_expired.send(sender=self.event, order=order)
        self.assertFalse(SeatAssignment.objects.filter(event=self.event).exists())

    def test_cancelling_one_order_leaves_other_orders_seats_alone(self):
        keep_order, keep_pos = self.order_with_position()
        self.answer(keep_pos, self.question, 'A-2')
        order_placed.send(sender=self.event, order=keep_order)
        gone_order, gone_pos = self.order_with_position()
        self.answer(gone_pos, self.question, 'A-1')
        order_placed.send(sender=self.event, order=gone_order)
        order_canceled.send(sender=self.event, order=gone_order)
        self.assertTrue(self.assigned('a2', keep_pos))
        self.assertFalse(self.assigned('a1', gone_pos))
