"""
Shared audit logic: find paid/pending order positions that have no recorded
SeatAssignment, resolve them to a real seat with the same tolerant label
matching used at checkout, and flag seats claimed by more than one order.
Used by both the `simpleseating_audit` management command and the control
panel audit page, so the two stay in sync.
"""
from collections import defaultdict

from django.db import transaction
from pretix.base.models import Order

from .models import SeatAssignment
from .seat_matching import build_label_index, match_seat_by_label


def run_seat_audit(event, cfg, fix=False):
    """
    Returns a dict:
      'unmatched': [(order, position, raw_label)]
          no assignment, and the typed label matches no known seat at all.
      'resolved': [{'order', 'position', 'seat_guid', 'raw_label', 'fixed'}]
          no assignment, but exactly one position unambiguously matches a
          free seat. 'fixed' is True if a SeatAssignment was created
          (only possible when fix=True).
      'conflicts': [{'seat_guid', 'already_sold_position_id', 'claims'}]
          more than one position (or one position plus an already-sold
          seat) claims the same seat; never auto-resolved.
    """
    label_index = build_label_index(event)
    assigned = {a.seat_guid: a for a in SeatAssignment.objects.filter(event=event)}
    assigned_position_ids = {a.order_position_id for a in assigned.values()}

    orders = Order.objects.filter(
        event=event, status__in=[Order.STATUS_PAID, Order.STATUS_PENDING]
    ).prefetch_related('positions', 'positions__answers')

    candidates_by_guid = defaultdict(list)
    unmatched = []

    for order in orders:
        for pos in order.positions.all():
            if pos.item_id != cfg.item_id or pos.id in assigned_position_ids:
                continue

            raw_label = None
            for ans in pos.answers.all():
                if ans.question_id == cfg.question_label_id and ans.answer:
                    raw_label = ans.answer.strip()
                    break

            seat = match_seat_by_label(label_index, raw_label) if raw_label else None
            if seat:
                candidates_by_guid[seat.seat_guid].append((order, pos, raw_label))
            else:
                unmatched.append((order, pos, raw_label))

    resolved = []
    conflicts = []

    for seat_guid, claims in candidates_by_guid.items():
        already_sold = assigned.get(seat_guid)
        if already_sold or len(claims) > 1:
            conflicts.append({
                'seat_guid': seat_guid,
                'already_sold_position_id': already_sold.order_position_id if already_sold else None,
                'claims': claims,
            })
            continue

        order, pos, raw_label = claims[0]
        fixed = False
        if fix:
            with transaction.atomic():
                _, created = SeatAssignment.objects.get_or_create(
                    event=event, seat_guid=seat_guid, defaults={'order_position_id': pos.id}
                )
            fixed = created
        resolved.append({
            'order': order,
            'position': pos,
            'seat_guid': seat_guid,
            'raw_label': raw_label,
            'fixed': fixed,
        })

    return {'unmatched': unmatched, 'resolved': resolved, 'conflicts': conflicts}
