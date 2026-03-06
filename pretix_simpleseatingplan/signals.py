
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

# Try to import order_position_meta_display if available
try:
    from pretix.base.signals import order_position_meta_display
    HAS_META_DISPLAY = True
except ImportError:
    HAS_META_DISPLAY = False

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
    if not cfg.svg or not cfg.question_label_id:
        return ''
    css = static('pretix_simpleseatingplan/frontend/seatpicker.css')
    from pretix.multidomain.urlreverse import eventreverse
    cfg_js = eventreverse(event, 'plugins:pretix_simpleseatingplan:config_js')
    return mark_safe(f'<link rel="stylesheet" href="{css}"><script src="{cfg_js}" defer></script>')

# Add order position meta display handler if available
if HAS_META_DISPLAY:
    @receiver(order_position_meta_display, dispatch_uid='simpleseating_order_position_meta_display')
    def order_position_meta_display_handler(sender, position, **kwargs):
        """Display seat number on tickets."""
        event = sender
        try:
            cfg = SeatingConfig.objects.get(event=event)
        except SeatingConfig.DoesNotExist:
            return

        if position.item_id != cfg.item_id:
            return

        # Get seat number from answer or meta_info
        seat_label = None

        # 1) From question answer (if exists)
        if cfg.question_label_id:
            ans = position.answers.filter(question_id=cfg.question_label_id).first()
            if ans and ans.answer:
                seat_label = ans.answer.strip()

        # 2) Fallback: from meta_info
        if not seat_label and hasattr(position, 'meta_info') and position.meta_info:
            seat_label = position.meta_info.get('seat_number')

        # 3) Fallback: from SeatAssignment -> Seat
        if not seat_label:
            assignment = SeatAssignment.objects.filter(event=event, order_position_id=position.id).first()
            if assignment:
                seat = Seat.objects.filter(event=event, seat_guid=assignment.seat_guid).first()
                if seat:
                    seat_label = seat.label

        if seat_label:
            return {
                'name': _('Seat'),
                'value': seat_label
            }

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
        if hold and catmap and getattr(p,'variation_id',None):
            seat = Seat.objects.filter(event=event, seat_guid=hold.seat_guid).first()
            if seat and seat.category and seat.category in catmap:
                expected = catmap[seat.category]
                if int(p.variation_id) != int(expected):
                    raise CartError(_('Seat category does not match ticket type. Please choose a seat in the correct price zone.'))

@receiver(validate_order, dispatch_uid='simpleseating_validate_order')
def on_validate_order(sender, positions, **kwargs):
    import logging
    event = sender
    logger = logging.getLogger(__name__)
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return
    if not cfg.item_id:
        return
    _purge_expired(event)
    catmap = _parse_category_map(cfg.category_variation_map)

    # Collect all positions needing seats and try to match them
    seat_positions = []
    for p in positions:
        if getattr(p, 'item_id', None) != cfg.item_id:
            continue
        seat_positions.append(p)

    if not seat_positions:
        return

    # Get all active holds for this event
    all_holds = list(SeatHold.objects.filter(event=event))
    used_hold_ids = set()

    for p in seat_positions:
        seat_guid = None

        # 1) Hold by exact cart_position_id
        for h in all_holds:
            if h.cart_position_id == p.id and h.id not in used_hold_ids and h.cart_position_id != 0:
                seat_guid = h.seat_guid
                used_hold_ids.add(h.id)
                break

        # 2) Seat label answer -> find seat_guid by label
        if not seat_guid and cfg.question_label_id:
            try:
                ans = p.answers.filter(question_id=cfg.question_label_id).first()
                if ans and ans.answer and ans.answer.strip():
                    seat = Seat.objects.filter(event=event, label=ans.answer.strip()).first()
                    if seat and not SeatAssignment.objects.filter(event=event, seat_guid=seat.seat_guid).exists():
                        seat_guid = seat.seat_guid
            except Exception as e:
                logger.warning(f"Error matching seat by label for position {p.id}: {e}")

        # 3) Fallback: any unmatched hold for this event (greedy matching)
        if not seat_guid:
            for h in all_holds:
                if h.id not in used_hold_ids:
                    if Seat.objects.filter(event=event, seat_guid=h.seat_guid).exists() \
                       and not SeatAssignment.objects.filter(event=event, seat_guid=h.seat_guid).exists():
                        seat_guid = h.seat_guid
                        used_hold_ids.add(h.id)
                        break

        if not seat_guid:
            logger.warning(f"No seat found for position {p.id} in event {event.slug}")
            raise OrderError(_('One or more tickets have no selected seat.'))

        if catmap and getattr(p, 'variation_id', None):
            try:
                seat = Seat.objects.filter(event=event, seat_guid=seat_guid).first()
                if seat and seat.category and seat.category in catmap:
                    expected = catmap[seat.category]
                    if int(p.variation_id) != int(expected):
                        raise OrderError(_('Seat category does not match ticket type.'))
            except OrderError:
                raise
            except Exception as e:
                logger.error(f"Error validating seat category for position {p.id}: {e}")

@receiver(order_placed, dispatch_uid='simpleseating_order_placed')
def on_order_placed(sender, order, **kwargs):
    event = sender
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return
    if not cfg.item_id:
        return
    _purge_expired(event)
    for op in order.positions.all():
        if op.item_id != cfg.item_id:
            continue
        seat_guid = None
        seat_label = None
        # 1) Seat label answer -> find seat_guid by label
        if cfg.question_label_id:
            for ans in op.answers.all():
                if ans.question_id == cfg.question_label_id and ans.answer:
                    seat = Seat.objects.filter(event=event, label=ans.answer.strip()).first()
                    if seat:
                        seat_guid = seat.seat_guid
                        seat_label = seat.label
                    break
        # 2) Fallback: find from SeatHold by cart_position_id
        if not seat_guid:
            hold = SeatHold.objects.filter(event=event, cart_position_id=op.id).first()
            if hold:
                seat = Seat.objects.filter(event=event, seat_guid=hold.seat_guid).first()
                if seat:
                    seat_guid = seat.seat_guid
                    seat_label = seat.label
        if not seat_guid:
            continue
        # Store seat info in position meta for custom display
        if seat_label:
            op.meta_info = op.meta_info or {}
            op.meta_info['seat_number'] = seat_label
            op.save(update_fields=['meta_info'])
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
