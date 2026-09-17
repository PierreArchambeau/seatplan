"""
Audit and (optionally) repair seat assignments for the simple seating plan
plugin.

Historically, orders could be placed with a manually-typed seat number that
didn't exactly match a known seat's label (different case, spacing, dashes,
leading zeros). validate_order used to fall back to grabbing an unrelated
free hold so the order would still go through, but order_placed had no such
fallback, so no SeatAssignment ever got created for that ticket: the order
was valid and paid, but the seat never showed as reserved on the plan.

This command finds paid/pending order positions that have no recorded
SeatAssignment, tries to resolve them to a real seat using the same
tolerant label matching now used at checkout time, and reports (or, with
--fix, creates) the missing assignments. When more than one order claims
the same seat, that is always only reported -- deciding which order
legitimately keeps the seat requires a human to look at the orders and
possibly contact a customer, so it is never resolved automatically.

See also the "Seat audit" page in the event's control panel (Simple seating
plan settings), which runs the same logic interactively.
"""
from django.core.management.base import BaseCommand

from pretix.base.models import Event

from ...audit import run_seat_audit
from ...models import SeatingConfig


class Command(BaseCommand):
    help = (
        "Find paid/pending orders whose seat was never recorded as sold on the "
        "simple seating plan (e.g. due to a manually-typed seat number that didn't "
        "match the expected format), and flag seats claimed by more than one order. "
        "With --fix, repairs unambiguous missing assignments; conflicts between "
        "orders are always only reported, never resolved automatically."
    )

    def add_arguments(self, parser):
        parser.add_argument('--event', help='Only audit this event, given as organizer/slug', default=None)
        parser.add_argument('--fix', action='store_true', help='Create missing SeatAssignments where exactly one order position unambiguously matches a free seat')

    def handle(self, *args, **options):
        events = Event.objects.filter(simpleseating_cfg__isnull=False)
        if options['event']:
            try:
                organizer_slug, event_slug = options['event'].split('/', 1)
            except ValueError:
                self.stderr.write(self.style.ERROR('--event must be given as organizer/slug'))
                return
            events = events.filter(organizer__slug=organizer_slug, slug=event_slug)

        fix = options['fix']
        total_missing = 0
        total_fixed = 0
        total_conflicts = 0
        total_unmatched = 0

        for event in events:
            try:
                cfg = event.simpleseating_cfg
            except SeatingConfig.DoesNotExist:
                continue
            if not cfg.item_id or not cfg.question_label_id:
                continue

            self.stdout.write(self.style.NOTICE(f'\n== {event.organizer.slug}/{event.slug} =='))

            result = run_seat_audit(event, cfg, fix=fix)

            for order, pos, raw_label in result['unmatched']:
                total_unmatched += 1
                self.stdout.write(self.style.WARNING(
                    f'  [NO MATCH] order {order.code} position {pos.id}: seat label "{raw_label}" matches no known seat -- needs manual review'
                ))

            for c in result['conflicts']:
                total_conflicts += 1
                self.stdout.write(self.style.ERROR(f'  [CONFLICT] seat {c["seat_guid"]} claimed by more than one order:'))
                if c['already_sold_position_id']:
                    self.stdout.write(f'      - already sold, position {c["already_sold_position_id"]}')
                for order, pos, raw_label in c['claims']:
                    self.stdout.write(f'      - order {order.code} position {pos.id} (typed "{raw_label}")')

            for r in result['resolved']:
                total_missing += 1
                if fix:
                    if r['fixed']:
                        total_fixed += 1
                        self.stdout.write(self.style.SUCCESS(
                            f'  [FIXED] order {r["order"].code} position {r["position"].id}: assigned seat {r["seat_guid"]} (typed "{r["raw_label"]}")'
                        ))
                    else:
                        self.stdout.write(self.style.ERROR(f'  [RACE] seat {r["seat_guid"]} got assigned concurrently, skipped'))
                else:
                    self.stdout.write(
                        f'  [MISSING] order {r["order"].code} position {r["position"].id}: would assign seat {r["seat_guid"]} (typed "{r["raw_label"]}")'
                    )

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(
            f'Total: {total_missing} missing assignment(s) resolvable ({total_fixed} fixed), '
            f'{total_unmatched} unmatched label(s), {total_conflicts} conflict(s) needing manual review.'
        ))
        if not fix and total_missing:
            self.stdout.write('Re-run with --fix to apply the unambiguous repairs above.')
