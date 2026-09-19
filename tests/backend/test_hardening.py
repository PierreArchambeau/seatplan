"""
Smaller hardening items: the polling endpoint must not write to the database
on every request, and the hold timer must stay within sane bounds.
"""
import datetime

from django.utils import timezone

from pretix_simpleseatingplan.models import SeatAssignment, SeatHold, SeatingConfig

from .base import SeatingTestCase


class StatusEndpointTests(SeatingTestCase):
    """seatpicker.js polls status every second per open checkout page."""

    def test_reports_sold_and_live_held_seats(self):
        SeatAssignment.objects.create(event=self.event, seat_guid='a1', order_position_id=1)
        SeatHold.objects.create(
            event=self.event, seat_guid='a2', cart_position_id=5,
            expires=timezone.now() + datetime.timedelta(minutes=5),
        )
        shopper, _ = self.new_shopper()
        data = shopper.get(self.url('status/')).json()
        self.assertEqual(data, {'sold': ['a1'], 'held': ['a2']})

    def test_expired_holds_are_not_reported_as_held(self):
        SeatHold.objects.create(
            event=self.event, seat_guid='a3', cart_position_id=5,
            expires=timezone.now() - datetime.timedelta(minutes=1),
        )
        shopper, _ = self.new_shopper()
        self.assertEqual(shopper.get(self.url('status/')).json()['held'], [])

    def test_polling_does_not_write_to_the_database(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        SeatHold.objects.create(
            event=self.event, seat_guid='a3', cart_position_id=5,
            expires=timezone.now() - datetime.timedelta(minutes=1),
        )
        shopper, _ = self.new_shopper()
        with CaptureQueriesContext(connection) as ctx:
            shopper.get(self.url('status/'))
        writes = [
            q['sql'] for q in ctx.captured_queries
            if q['sql'].lstrip().upper().startswith(('DELETE', 'UPDATE', 'INSERT')) and 'django_session' not in q['sql']
        ]
        self.assertEqual(writes, [], 'status must be read-only apart from the session')
        # purging is left to hold/validate/the periodic task, not to every poll
        self.assertEqual(SeatHold.objects.filter(event=self.event, seat_guid='a3').count(), 1)

    def test_visitor_without_a_session_is_refused(self):
        from django.test import Client
        self.assertEqual(Client().get(self.url('status/')).status_code, 403)


class HoldMinutesBoundsTests(SeatingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_client()

    def post_minutes(self, value):
        return self.upload_plan(self.admin, hold_minutes=value)

    def test_reasonable_values_are_accepted(self):
        for value in (1, 10, 120):
            resp = self.post_minutes(value)
            self.assertEqual(resp.status_code, 302, value)
            self.assertEqual(SeatingConfig.objects.get(event=self.event).hold_minutes, value)

    def test_zero_and_absurd_values_are_rejected(self):
        for value in (0, -5, 121, 10 ** 9):
            resp = self.post_minutes(value)
            self.assertEqual(resp.status_code, 200, value)  # form redisplayed with an error
            self.assertEqual(SeatingConfig.objects.get(event=self.event).hold_minutes, 10, 'unchanged after %r' % value)


class PeriodicTaskTests(SeatingTestCase):
    """`runperiodic` calls the receivers with no django-scopes scope active."""

    def test_purges_expired_holds_across_events_and_keeps_live_ones(self):
        from pretix_simpleseatingplan.signals import on_periodic
        from .base import no_active_scope
        SeatHold.objects.create(
            event=self.event, seat_guid='a1', cart_position_id=1,
            expires=timezone.now() - datetime.timedelta(minutes=1),
        )
        SeatHold.objects.create(
            event=self.event, seat_guid='a2', cart_position_id=2,
            expires=timezone.now() + datetime.timedelta(minutes=5),
        )
        with no_active_scope():
            on_periodic(sender=None)
        self.assertEqual(list(SeatHold.objects.filter(event=self.event).values_list('seat_guid', flat=True)), ['a2'])
