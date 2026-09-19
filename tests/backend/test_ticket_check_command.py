"""
`simpleseating_ticket_check`: a diagnostic to run on a server when the seating
plan is missing from a ticket PDF. It walks the whole chain (cairo, the stored
plan, the ticket layout, the seat of the order, a real PDF) and says which link
is broken, because the symptom is always the same -- a grey box or nothing --
whatever the cause.
"""
import datetime
import json
import sys
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.utils import timezone
from pretix.base.models import CachedTicket, Order, OrderPosition
from pretix.base.signals import order_placed
from pretix.plugins.ticketoutputpdf.models import TicketLayout, TicketLayoutItem

from pretix_simpleseatingplan.models import SeatingConfig

from .base import no_active_scope
from .test_order_flow import OrderFlowTestCase

PLAN_ELEMENT = {
    'type': 'imagearea', 'left': '10', 'bottom': '20', 'width': '120', 'height': '80',
    'content': 'simpleseating_plan', 'title': 'Seat plan',
}
OTHER_ELEMENT = {
    'type': 'imagearea', 'left': '10', 'bottom': '20', 'width': '50', 'height': '50', 'content': 'other',
}


class TicketCheckTestCase(OrderFlowTestCase):
    def setUp(self):
        super().setUp()
        self.event.plugins += ',pretix.plugins.ticketoutputpdf'
        self.event.save()
        self.organizer.plugins = (self.organizer.plugins or '') + ',pretix.plugins.ticketoutputpdf'
        self.organizer.save()

    def layout(self, elements, default=True, item=None):
        layout = TicketLayout.objects.create(event=self.event, name='Layout', default=default, layout=json.dumps(elements))
        if item is not None:
            TicketLayoutItem.objects.create(item=item, layout=layout, sales_channel=self.channel)
        return layout

    def paid_order(self, label='A-2'):
        order, pos = self.order_with_position(status=Order.STATUS_PAID)
        if label is not None:
            self.answer(pos, self.question, label)
        order_placed.send(sender=self.event, order=order)
        return order, pos

    def check(self, *args, expect_failure=False):
        out = StringIO()
        with no_active_scope():
            if expect_failure:
                with self.assertRaises(CommandError):
                    call_command('simpleseating_ticket_check', 'org/concert', *args, stdout=out, stderr=StringIO())
            else:
                call_command('simpleseating_ticket_check', 'org/concert', *args, stdout=out, stderr=StringIO())
        return out.getvalue()


class HealthySetupTests(TicketCheckTestCase):
    def test_everything_ok_reports_no_failure_and_a_real_plan_in_the_pdf(self):
        self.layout([PLAN_ELEMENT])
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code)
        self.assertNotIn('[FAIL]', out)
        self.assertIn('[ OK ] cairo', out)
        self.assertIn('[ OK ] stored plan', out)
        self.assertIn('[ OK ] ticket layout', out)
        self.assertIn('[ OK ] seat of the order: A-2', out)
        self.assertIn('[ OK ] plan image', out)
        self.assertIn('[ OK ] ticket PDF', out)

    def test_the_rendered_plan_can_be_saved_for_a_look(self):
        import tempfile, os
        self.layout([PLAN_ELEMENT])
        order, _pos = self.paid_order('A-2')
        path = os.path.join(tempfile.mkdtemp(), 'plan.png')
        self.check('--order', order.code, '--png', path)
        with open(path, 'rb') as f:
            self.assertTrue(f.read().startswith(b'\x89PNG'))

    def test_without_an_order_only_the_server_side_checks_run(self):
        self.layout([PLAN_ELEMENT])
        out = self.check()
        self.assertIn('[ OK ] cairo', out)
        self.assertNotIn('seat of the order', out)
        self.assertIn('no --order given', out)

    def test_it_says_where_it_runs_because_celery_workers_must_match(self):
        self.layout([PLAN_ELEMENT])
        out = self.check()
        self.assertIn(sys.executable, out)
        self.assertIn('Celery', out)


class BrokenLinksTests(TicketCheckTestCase):
    def test_layout_without_the_plan_element_is_the_reported_cause(self):
        self.layout([OTHER_ELEMENT])
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code, expect_failure=True)
        self.assertIn('[FAIL] ticket layout', out)
        self.assertIn('simpleseating_plan', out)
        self.assertIn('[FAIL] ticket PDF', out)
        self.assertIn('[ OK ] cairo', out, 'the other links are fine and reported as such')
        self.assertIn('[ OK ] plan image', out)

    def test_plan_element_in_another_layout_than_the_one_the_item_uses_is_found(self):
        self.layout([OTHER_ELEMENT], default=True)
        self.layout([PLAN_ELEMENT], default=False)  # exists, but is assigned to no item
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code, expect_failure=True)
        self.assertIn('[FAIL] ticket layout', out)
        self.assertIn('not the layout used by this ticket', out)

    def test_item_specific_layout_wins_over_the_default(self):
        self.layout([OTHER_ELEMENT], default=True)
        self.layout([PLAN_ELEMENT], default=False, item=self.item)
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code)
        self.assertIn('[ OK ] ticket layout', out)
        self.assertIn('[ OK ] ticket PDF', out)

    def test_missing_libcairo_is_reported_with_the_python_in_use(self):
        self.layout([PLAN_ELEMENT])
        with mock.patch('cairocffi.cairo_version_string', side_effect=OSError('no library called "cairo-2" was found')):
            out = self.check(expect_failure=True)
        self.assertIn('[FAIL] cairo', out)
        self.assertIn('no library called "cairo-2"', out)
        self.assertIn('libcairo', out)

    def test_cairosvg_not_importable_is_reported(self):
        self.layout([PLAN_ELEMENT])
        with mock.patch.dict(sys.modules, {'cairosvg': None}):
            out = self.check(expect_failure=True)
        self.assertIn('[FAIL] cairo', out)
        self.assertIn('cairosvg is not installed', out)

    def test_a_stored_plan_the_sanitizer_rejects_is_reported_with_the_reason(self):
        self.layout([PLAN_ELEMENT])
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(
            svg='<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY ns "http://www.w3.org/2000/svg">]><svg xmlns="&ns;"/>')
        out = self.check(expect_failure=True)
        self.assertIn('[FAIL] stored plan', out)
        self.assertIn('entities', out)

    def test_a_plan_without_any_seat_id_is_reported_with_the_prefix_in_use(self):
        self.layout([PLAN_ELEMENT])
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(seat_id_prefix='chair-')
        out = self.check(expect_failure=True)
        self.assertIn('[FAIL] stored plan', out)
        self.assertIn('"chair-"', out)

    def test_an_empty_plan_is_reported(self):
        self.layout([PLAN_ELEMENT])
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg='')
        out = self.check(expect_failure=True)
        self.assertIn('[FAIL] stored plan', out)

    def test_an_order_without_a_resolvable_seat_points_to_the_audit(self):
        self.layout([PLAN_ELEMENT])
        order, _pos = self.paid_order(None)
        out = self.check('--order', order.code, expect_failure=True)
        self.assertIn('[FAIL] seat of the order', out)
        self.assertIn('simpleseating_audit', out)

    def test_a_seat_missing_from_the_plan_is_reported(self):
        self.layout([PLAN_ELEMENT])
        SeatingConfig.objects.filter(pk=self.cfg.pk).update(svg=self.cfg.svg.replace('seat-a2', 'seat-zz'))
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code, expect_failure=True)
        self.assertIn('[FAIL] plan image', out)

    def test_the_ticket_plugin_being_off_is_reported(self):
        self.layout([PLAN_ELEMENT])
        self.event.plugins = 'pretix_simpleseatingplan'
        self.event.save()
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code, expect_failure=True)
        self.assertIn('ticketoutputpdf', out)

    def test_unpaid_orders_are_flagged_because_no_ticket_is_issued_for_them(self):
        self.layout([PLAN_ELEMENT])
        order, pos = self.order_with_position(status=Order.STATUS_PENDING)
        self.answer(pos, self.question, 'A-2')
        order_placed.send(sender=self.event, order=order)
        out = self.check('--order', order.code)
        self.assertIn('[WARN] order status', out)


POWERED_BY = {
    'type': 'poweredby', 'left': '150', 'bottom': '10', 'size': '30', 'content': 'dark',
}


class PlanIsReallyOnThePageTests(TicketCheckTestCase):
    """Counting images is not enough: a layout can carry other images (the
    "powered by" logo, a picture). The plan must be shown to be the one that
    is there, by comparing the ticket with and without its plan element."""

    def test_other_images_on_the_ticket_do_not_hide_a_missing_plan(self):
        self.layout([PLAN_ELEMENT, POWERED_BY])
        order, _pos = self.paid_order('A-2')
        with mock.patch('pretix_simpleseatingplan.signals.render_seat_plan_png', return_value=None):
            out = self.check('--order', order.code, expect_failure=True)
        self.assertIn('[FAIL] ticket PDF', out)
        self.assertIn('no plan image', out)

    def test_the_plan_is_recognised_next_to_other_images(self):
        self.layout([PLAN_ELEMENT, POWERED_BY])
        order, _pos = self.paid_order('A-2')
        out = self.check('--order', order.code)
        self.assertIn('[ OK ] ticket PDF', out)
        self.assertIn('the plan is printed', out)


class WorkerCheckTests(TicketCheckTestCase):
    """Tickets are generated by Celery workers, which may run in another
    environment than the shell the command runs in."""

    def setUp(self):
        super().setUp()
        self.layout([PLAN_ELEMENT])
        self.order, self.pos = self.paid_order('A-2')

    def test_without_celery_the_check_says_the_web_process_generates_tickets(self):
        out = self.check('--order', self.order.code, '--worker')
        self.assertIn('Celery is not configured', out)
        self.assertNotIn('[FAIL]', out)

    def test_a_healthy_worker_reports_its_python_host_and_a_drawn_plan(self):
        from django.test import override_settings
        with override_settings(HAS_CELERY=True):
            out = self.check('--order', self.order.code, '--worker')
        self.assertIn('[ OK ] celery worker', out)
        self.assertIn('cairo', out.split('celery worker', 1)[1])
        self.assertIn('drew the plan for seat A-2', out)
        self.assertNotIn('[FAIL]', out)

    def test_cairo_missing_only_in_the_worker_is_reported_as_such(self):
        from django.test import override_settings
        with override_settings(HAS_CELERY=True),                 mock.patch('cairocffi.cairo_version_string', side_effect=OSError('no library called "cairo-2"')):
            out = self.check('--order', self.order.code, '--worker', expect_failure=True)
        self.assertIn('[FAIL] celery worker', out)
        self.assertIn('no library called "cairo-2"', out)

    def test_a_worker_that_cannot_be_reached_or_runs_old_code_is_reported(self):
        from django.test import override_settings
        from pretix_simpleseatingplan import tasks
        with override_settings(HAS_CELERY=True),                 mock.patch.object(tasks.worker_check, 'apply_async', side_effect=RuntimeError('Received unregistered task')):
            out = self.check('--order', self.order.code, '--worker', expect_failure=True)
        self.assertIn('[FAIL] celery worker', out)
        self.assertIn('Received unregistered task', out)
        self.assertIn('restart', out)

    def test_a_worker_running_another_python_is_flagged(self):
        from django.test import override_settings
        from pretix_simpleseatingplan import tasks
        real = tasks.worker_check_impl

        def other_python(*args, **kwargs):
            result = real(*args, **kwargs)
            result['python'] = '/usr/bin/python3'
            return result
        with override_settings(HAS_CELERY=True), mock.patch.object(tasks, 'worker_check_impl', other_python):
            out = self.check('--order', self.order.code, '--worker')
        self.assertIn('[WARN] celery worker', out)
        self.assertIn('/usr/bin/python3', out)


class CacheTests(TicketCheckTestCase):
    def cached(self, pos):
        return CachedTicket.objects.create(order_position=pos, provider='pdf', type='application/pdf', extension='.pdf')

    def test_a_cached_ticket_is_reported_as_a_possible_stale_copy(self):
        self.layout([PLAN_ELEMENT])
        order, pos = self.paid_order('A-2')
        self.cached(pos)
        out = self.check('--order', order.code)
        self.assertIn('cached ticket', out)
        self.assertIn('--clear-cache', out)
        self.assertEqual(CachedTicket.objects.filter(order_position=pos).count(), 1, 'reporting must not delete anything')

    def test_clear_cache_deletes_only_this_orders_cached_tickets(self):
        self.layout([PLAN_ELEMENT])
        order, pos = self.paid_order('A-2')
        other_order, other_pos = self.paid_order('A-3')
        self.cached(pos)
        self.cached(other_pos)
        self.check('--order', order.code, '--clear-cache')
        self.assertFalse(CachedTicket.objects.filter(order_position=pos).exists())
        self.assertTrue(CachedTicket.objects.filter(order_position=other_pos).exists())


class ArgumentTests(TicketCheckTestCase):
    def test_unknown_event_or_order_are_clean_errors(self):
        with no_active_scope():
            with self.assertRaises(CommandError):
                call_command('simpleseating_ticket_check', 'org/nope', stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command('simpleseating_ticket_check', 'no-slash', stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command('simpleseating_ticket_check', 'org/concert', '--order', 'ZZZZZ', stdout=StringIO())

    def test_position_option_selects_the_ticket_to_check(self):
        self.layout([PLAN_ELEMENT])
        order, first = self.order_with_position(status=Order.STATUS_PAID)
        second = OrderPosition.objects.create(order=order, item=self.item, price=10, tax_rate=0, tax_value=0, positionid=2)
        self.answer(first, self.question, 'A-1')
        self.answer(second, self.question, 'A-3')
        order_placed.send(sender=self.event, order=order)
        out = self.check('--order', order.code, '--position', '2')
        self.assertIn('seat of the order: A-3', out)
