"""
Keeping pretix's cache of generated ticket PDFs honest.

pretix stores every generated ticket (CachedTicket) and only drops it when the
order, the item or the ticket layout changes. Seat assignments and the plan
are this plugin's data, which pretix cannot see, so whenever the plugin
changes one of them it has to ask pretix to invalidate the affected tickets;
otherwise a ticket generated before the seat was recorded (or before a new
plan was uploaded) keeps showing no plan, or the old one.
"""
from django.db import transaction


def invalidate_tickets(event, order=None):
    """Drop the cached tickets of one order, or of the whole event.

    Deferred until the surrounding transaction commits, so the Celery task
    (which regenerates from the database) never sees the old data."""
    from pretix.base.services import tickets

    kwargs = {'event': event.pk}
    if order is not None:
        kwargs['order'] = order.pk
    transaction.on_commit(lambda: tickets.invalidate_cache.apply_async(kwargs=kwargs))
