"""
Seat claiming: the one place that decides who may hold a seat.

SeatHold has a unique (event, seat_guid) constraint, so creating a hold is an
atomic "first come, first served" operation even with concurrent requests.
Both the seat picker's hold endpoint and the final order validation go
through claim_seat(), so a seat somebody else is in the middle of buying is
refused server-side too -- not only greyed out by the front-end.
"""
from django.db import IntegrityError, transaction
from pretix.base.models import CartPosition

from .models import SeatHold


def _is_orphaned(hold):
    """A hold whose cart position no longer exists (cart expired or deleted,
    or a legacy hold created without one) protects nobody: its owner is gone,
    even though the hold's own timer has not run out yet."""
    if not hold.cart_position_id:
        return True
    return not CartPosition.objects.filter(pk=hold.cart_position_id).exists()


def claim_seat(event, seat_guid, cartpos_id, expires):
    """Try to hold `seat_guid` for cart position `cartpos_id` until `expires`.

    Returns True if the seat is now held by that cart position (newly taken,
    renewed, or taken over from an orphaned hold) and False if a live hold of
    another cart position stands in the way. Expired holds must have been
    purged by the caller beforehand.
    """
    try:
        with transaction.atomic():
            hold, created = SeatHold.objects.get_or_create(
                event=event, seat_guid=seat_guid,
                defaults={'cart_position_id': cartpos_id, 'expires': expires},
            )
    except IntegrityError:
        # Lost a creation race twice in a row; whoever won owns the seat.
        return False
    if created:
        return True
    if hold.cart_position_id == cartpos_id:
        SeatHold.objects.filter(pk=hold.pk).update(expires=expires)
        return True
    if _is_orphaned(hold):
        # Conditional update: if two buyers race for the same orphaned hold,
        # only one of them matches the old owner and wins.
        taken = SeatHold.objects.filter(
            pk=hold.pk, cart_position_id=hold.cart_position_id,
        ).update(cart_position_id=cartpos_id, expires=expires)
        return taken == 1
    return False
