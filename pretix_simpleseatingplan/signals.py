
from django.dispatch import receiver
from django.templatetags.static import static
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from pretix.base.services.cart import CartError
from pretix.base.services.orders import OrderError
from pretix.base.signals import validate_cart, validate_order, order_placed, order_canceled, order_expired, periodic_task
from pretix.control.signals import nav_event_settings
from pretix.presale.signals import html_head
from .models import SeatingConfig, SeatHold, SeatAssignment, Seat
from pretix.presale.signals import html_head  # ⬅️ presale, pas base

@receiver(nav_event_settings, dispatch_uid='simpleseating_nav_event_settings')
def nav_settings(sender, request, **kwargs):
    if not request.user.has_event_permission(request.organizer, request.event, 'can_change_settings'):
        return []
    return [{
        'label': _('Simple seating plan (SVG/JSON)'),
        'url': reverse('plugins:pretix_simpleseatingplan:settings', kwargs={'organizer': request.organizer.slug, 'event': request.event.slug}),
        'active': request.path_info.endswith('/simpleseatingplan/'),
    }]

@receiver(html_head, dispatch_uid='simpleseating_html_head')
def inject_presale_head(sender, request=None, **kwargs):
    event = sender
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return ''
    if not cfg.svg or not cfg.question_guid_id or not cfg.question_label_id:
        return ''
    css = static('pretix_simpleseatingplan/seatpicker.css')
    from pretix.multidomain.urlreverse import eventreverse
    cfg_js = eventreverse(event, 'plugins:pretix_simpleseatingplan:config_js')
    return mark_safe(f'<link rel="stylesheet" href="{css}"><script src="{cfg_js}" defer></script>')

from django.utils import timezone

def _purge_expired(event):
    SeatHold.objects.filter(event=event, expires__lte=timezone.now()).delete()

def _parse_category_map(text):
    res = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip(); v = v.strip()
        try:
            res[k] = int(v)
        except ValueError:
            continue
    return res

@receiver(validate_cart, dispatch_uid='simpleseating_validate_cart')
def on_validate_cart(sender, positions, **kwargs):
    event = sender
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return
    if not cfg.item_id:
        return
    _purge_expired(event)
    catmap = _parse_category_map(cfg.category_variation_map)
    for p in positions:
        if getattr(p,'item_id',None) != cfg.item_id:
            continue
        hold = SeatHold.objects.filter(event=event, cart_position_id=p.id).first()
        # if not hold:
        #     raise CartError(_('Please choose a seat for each ticket.'))
        if catmap and getattr(p,'variation_id',None):
            seat = Seat.objects.filter(event=event, seat_guid=hold.seat_guid).first()
            if seat and seat.category and seat.category in catmap:
                expected = catmap[seat.category]
                if int(p.variation_id) != int(expected):
                    raise CartError(_('Seat category does not match ticket type. Please choose a seat in the correct price zone.'))

@receiver(validate_order, dispatch_uid='simpleseating_validate_order')
def on_validate_order(sender, positions, **kwargs):
    event = sender
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return
    if not cfg.item_id:
        return
    _purge_expired(event)
    catmap = _parse_category_map(cfg.category_variation_map)
    for p in positions:
        if getattr(p,'item_id',None) != cfg.item_id:
            continue
        hold = SeatHold.objects.filter(event=event, cart_position_id=p.id).first()
        if not hold:
            raise OrderError(_('One or more tickets have no selected seat.'))
        if catmap and getattr(p,'variation_id',None):
            seat = Seat.objects.filter(event=event, seat_guid=hold.seat_guid).first()
            if seat and seat.category and seat.category in catmap:
                expected = catmap[seat.category]
                if int(p.variation_id) != int(expected):
                    raise OrderError(_('Seat category does not match ticket type.'))

@receiver(order_placed, dispatch_uid='simpleseating_order_placed')
def on_order_placed(sender, order, **kwargs):
    event = sender
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return
    if not cfg.item_id or not cfg.question_guid_id:
        return
    _purge_expired(event)
    for op in order.positions.all():
        if op.item_id != cfg.item_id:
            continue
        seat_guid = None
        for ans in op.answers.all():
            if ans.question_id == cfg.question_guid_id:
                seat_guid = ans.answer
                break
        if not seat_guid:
            continue
        SeatAssignment.objects.get_or_create(event=event, seat_guid=seat_guid, defaults={'order_position_id': op.id})
    SeatHold.objects.filter(event=event, seat_guid__in=SeatAssignment.objects.filter(event=event).values_list('seat_guid', flat=True)).delete()

@receiver(order_canceled, dispatch_uid='simpleseating_order_canceled')
def on_order_canceled(sender, order, **kwargs):
    event = sender
    SeatAssignment.objects.filter(event=event, order_position_id__in=order.positions.values_list('id', flat=True)).delete()

@receiver(order_expired, dispatch_uid='simpleseating_order_expired')
def on_order_expired(sender, order, **kwargs):
    event = sender
    SeatAssignment.objects.filter(event=event, order_position_id__in=order.positions.values_list('id', flat=True)).delete()

@receiver(periodic_task, dispatch_uid='simpleseating_periodic')
def on_periodic(sender, **kwargs):
    from pretix.base.models import Event
    for ev in Event.objects.all().iterator():
        _purge_expired(ev)



@receiver(html_head, dispatch_uid="simpleseatingplan_html_head")

def add_assets_to_presale(sender, **kwargs):
    # 1) chemins statiques vers les assets packagés par le plugin
    css_href = static('pretix_simpleseatingplan/frontend/seatpicker.css')
    js_src   = static('pretix_simpleseatingplan/frontend/seatpicker.js')


    # 3) retourner des BALISES complètes, dans le bon ordre
    return (
        f'<link rel="stylesheet" href="{css_href}">\n'
        f'<script src="{js_src}" defer></script>\n'
    )


