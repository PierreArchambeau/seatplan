
import hashlib

from django.core.files.base import ContentFile
from django.dispatch import receiver
from django.templatetags.static import static
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from pretix.base.services.cart import CartError
from pretix.base.services.orders import OrderError
from pretix.base.signals import validate_cart, validate_order, order_placed, order_canceled, order_expired, order_modified, periodic_task
from pretix.control.signals import nav_event_settings
from pretix.presale.signals import html_head
from .models import SeatingConfig, SeatHold, SeatAssignment, Seat
from .seat_matching import build_label_index, match_seat_by_label, find_misplaced_seat_answer
from .ticket_image import resolve_seat_for_position, render_seat_plan_png

# Try to import order_position_meta_display if available
try:
    from pretix.base.signals import order_position_meta_display
    HAS_META_DISPLAY = True
except ImportError:
    HAS_META_DISPLAY = False

# Try to import layout_image_variables if available (ticket PDF image placeholders)
try:
    from pretix.base.signals import layout_image_variables
    HAS_LAYOUT_IMAGE_VARIABLES = True
except ImportError:
    HAS_LAYOUT_IMAGE_VARIABLES = False

@receiver(nav_event_settings, dispatch_uid='simpleseating_nav_event_settings')
def nav_settings(sender, request, **kwargs):
    if not request.user.has_event_permission(request.organizer, request.event, 'can_change_settings'):
        return []
    return [{
        'label': _('Simple seating plan (SVG/JSON)'),
        'url': reverse('plugins:pretix_simpleseatingplan:settings', kwargs={'organizer': request.organizer.slug, 'event': request.event.slug}),
        'active': request.path_info.endswith('/simpleseatingplan/'),
    }, {
        'label': _('Seat audit'),
        'url': reverse('plugins:pretix_simpleseatingplan:audit', kwargs={'organizer': request.organizer.slug, 'event': request.event.slug}),
        'active': request.path_info.endswith('/simpleseatingplan/audit/'),
    }]

@receiver(html_head, dispatch_uid='simpleseating_html_head')
def inject_presale_head(sender, request=None, **kwargs):
    from pretix.base.models import Question
    event = sender
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return ''
    if not cfg.svg or not cfg.question_label_id:
        return ''
    # Verify the question actually exists in the event
    if not Question.objects.filter(event=event, pk=cfg.question_label_id).exists():
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
        import json as json_module
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

        # 2) Fallback: from meta_info (parse JSON string)
        if not seat_label and position.meta_info:
            try:
                meta_dict = json_module.loads(position.meta_info) if isinstance(position.meta_info, str) else position.meta_info
                if isinstance(meta_dict, dict):
                    seat_label = meta_dict.get('seat_number')
            except (ValueError, TypeError):
                pass

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

# Add a ticket PDF image placeholder showing the seating plan with the
# purchased seat highlighted, if available.
if HAS_LAYOUT_IMAGE_VARIABLES:
    @receiver(layout_image_variables, dispatch_uid='simpleseating_layout_image_variables')
    def layout_image_variables_handler(sender, **kwargs):
        event = sender

        def _cfg_for(op):
            try:
                cfg = SeatingConfig.objects.get(event=event)
            except SeatingConfig.DoesNotExist:
                return None
            if not cfg.svg or op.item_id != cfg.item_id:
                return None
            return cfg

        def evaluate(op, order, ev):
            cfg = _cfg_for(op)
            if not cfg:
                return None
            seat_guid, seat_label = resolve_seat_for_position(event, cfg, op)
            if not seat_guid:
                import logging
                logging.getLogger(__name__).warning(
                    "simpleseatingplan: no seat could be resolved for position %s (order %s) in event %s -- "
                    "ticket image skipped", op.id, order.code, event.slug,
                )
                return None
            png_bytes = render_seat_plan_png(event, cfg, seat_guid, seat_label)
            if not png_bytes:
                return None
            return ContentFile(png_bytes, name=f'seatplan-{seat_guid}.png')

        def etag(op, order, ev):
            cfg = _cfg_for(op)
            if not cfg:
                return None
            seat_guid, _unused = resolve_seat_for_position(event, cfg, op)
            if not seat_guid:
                return None
            return hashlib.sha1(f'{seat_guid}|{cfg.svg}'.encode('utf-8')).hexdigest()

        return {
            'simpleseating_plan': {
                'label': _('Seating plan with purchased seat highlighted'),
                'evaluate': evaluate,
                'etag': etag,
            }
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
    label_index = build_label_index(event) if cfg.question_label_id else {}
    for p in positions:
        if getattr(p,'item_id',None) != cfg.item_id:
            continue

        if cfg.question_label_id:
            misplaced = find_misplaced_seat_answer(p, cfg, label_index)
            if misplaced:
                raise CartError(_('"%(value)s" looks like a seat number but was entered in the "%(question)s" field. Please put the seat number only in the "Seat" field.') % {
                    'value': misplaced.answer, 'question': misplaced.question.question,
                })

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
    label_index = build_label_index(event)
    # Seats already sold (from other, previously placed orders)
    assigned_guids = set(SeatAssignment.objects.filter(event=event).values_list('seat_guid', flat=True))

    for p in seat_positions:
        seat_guid = None

        if cfg.question_label_id:
            misplaced = find_misplaced_seat_answer(p, cfg, label_index)
            if misplaced:
                raise OrderError(_('"%(value)s" looks like a seat number but was entered in the "%(question)s" field. Please put the seat number only in the "Seat" field.') % {
                    'value': misplaced.answer, 'question': misplaced.question.question,
                })

        # 1) Hold by exact cart_position_id: the seat the customer actually
        #    clicked on the plan for this specific ticket.
        for h in all_holds:
            if h.cart_position_id == p.id and h.id not in used_hold_ids and h.cart_position_id != 0:
                seat_guid = h.seat_guid
                used_hold_ids.add(h.id)
                break

        # When resolved via the hold, force the "Seat" answer text to match
        # the held seat's canonical label. This matters because order
        # creation (OrderPosition.transform_cart_positions) copies this
        # CartPosition into a brand new OrderPosition with its own,
        # unrelated primary key -- order_placed can no longer look this
        # hold up by cart_position_id afterwards, so it re-derives the seat
        # purely from this answer text. Without this sync, a hold that
        # doesn't match whatever text happens to be in the field (e.g. the
        # customer clicked a seat, then edited the text field by hand
        # without it registering) would pass validation here but leave the
        # seat unassigned after payment -- exactly the "unmatched" bug this
        # fixes.
        if seat_guid and cfg.question_label_id:
            seat_obj = Seat.objects.filter(event=event, seat_guid=seat_guid).first()
            if seat_obj and seat_obj.label:
                from pretix.base.models import QuestionAnswer
                QuestionAnswer.objects.update_or_create(
                    cartposition=p, question_id=cfg.question_label_id,
                    defaults={'answer': seat_obj.label},
                )

        # 2) Seat label answer -> find seat_guid by normalized label match.
        #    Tolerates manual entry with different case/spacing/dashes
        #    (e.g. "a12" / "A 12" / "A-012" all match seat "A-12").
        if not seat_guid and cfg.question_label_id:
            try:
                ans = p.answers.filter(question_id=cfg.question_label_id).first()
                if ans and ans.answer and ans.answer.strip():
                    seat = match_seat_by_label(label_index, ans.answer)
                    if seat and seat.seat_guid not in assigned_guids:
                        seat_guid = seat.seat_guid
            except Exception as e:
                logger.warning(f"Error matching seat by label for position {p.id}: {e}")

        # No silent fallback to an unrelated hold here: matching a position
        # to a seat nobody actually selected for it would let the order
        # pass validation while order_placed later fails to create the
        # matching SeatAssignment, leaving the seat looking free forever.
        if not seat_guid:
            logger.warning(f"No seat found for position {p.id} in event {event.slug}")
            raise OrderError(_('Your seat number does not match any available seat. Please select a seat on the seating plan.'))

        assigned_guids.add(seat_guid)

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
    import json as json_module
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
    label_index = build_label_index(event)
    assigned_guids = set(SeatAssignment.objects.filter(event=event).values_list('seat_guid', flat=True))

    for op in order.positions.all():
        if op.item_id != cfg.item_id:
            continue
        seat_guid = None
        seat_label = None

        # Match by the "Seat" answer text. Note: there is deliberately no
        # "hold by cart_position_id" lookup here (there used to be one) --
        # by the time order_placed fires, `op` is a brand new OrderPosition
        # with its own primary key, unrelated to the CartPosition.id the
        # SeatHold was recorded against (OrderPosition.transform_cart_positions
        # creates fresh rows, it doesn't preserve the id). That lookup could
        # therefore never actually find the hold. Instead, on_validate_order
        # now forces the answer text to match the held seat's label before
        # the order is created, so this text-based match is reliable.
        if cfg.question_label_id:
            ans = op.answers.filter(question_id=cfg.question_label_id).first()
            if ans and ans.answer:
                seat = match_seat_by_label(label_index, ans.answer)
                if seat and seat.seat_guid not in assigned_guids:
                    seat_guid = seat.seat_guid
                    seat_label = seat.label

        if not seat_guid:
            # validate_order should have caught this before payment; if we
            # still end up here, log loudly so it can be repaired instead
            # of silently leaving the seat looking free on the plan.
            logger.error(f"order_placed: no seat could be assigned for position {op.id} (order {order.code}) in event {event.slug}")
            continue

        assigned_guids.add(seat_guid)
        # Store seat info in position meta for custom display (as JSON string)
        if seat_label:
            try:
                meta_dict = json_module.loads(op.meta_info) if isinstance(op.meta_info, str) else (op.meta_info or {})
                if not isinstance(meta_dict, dict):
                    meta_dict = {}
            except (ValueError, TypeError):
                meta_dict = {}
            meta_dict['seat_number'] = seat_label
            op.meta_info = json_module.dumps(meta_dict)
            op.save(update_fields=['meta_info'])
        SeatAssignment.objects.get_or_create(event=event, seat_guid=seat_guid, defaults={'order_position_id': op.id})
    SeatHold.objects.filter(event=event, seat_guid__in=SeatAssignment.objects.filter(event=event).values_list('seat_guid', flat=True)).delete()

@receiver(order_modified, dispatch_uid='simpleseating_order_modified')
def on_order_modified(sender, order, **kwargs):
    """
    Pretix sends this signal whenever user-entered information on an
    already-placed order is edited -- including question answers changed
    by staff in the control panel ("Modify" on the order detail page), by
    the customer through self-service order changes, or via the API. Since
    the "Seat" answer can be edited this way, the SeatAssignment table (and
    therefore what shows as sold on the plan) needs to be kept in sync with
    whatever the answer says now.

    This can only react after the fact -- the edit is already saved by the
    time this fires -- so ambiguous cases (edited to a seat already held by
    another position, or to a label matching no seat at all) are left
    untouched and logged rather than guessed at; run simpleseating_audit to
    review those.
    """
    import logging
    event = sender
    logger = logging.getLogger(__name__)
    try:
        cfg = SeatingConfig.objects.get(event=event)
    except SeatingConfig.DoesNotExist:
        return
    if not cfg.item_id or not cfg.question_label_id:
        return

    label_index = build_label_index(event)
    assignments = {a.order_position_id: a for a in SeatAssignment.objects.filter(event=event)}
    assigned_guids = {a.seat_guid: a for a in assignments.values()}

    for op in order.positions.all():
        if op.item_id != cfg.item_id:
            continue

        ans = op.answers.filter(question_id=cfg.question_label_id).first()
        raw_label = ans.answer.strip() if ans and ans.answer else ''
        new_seat = match_seat_by_label(label_index, raw_label) if raw_label else None

        current = assignments.get(op.id)
        current_guid = current.seat_guid if current else None
        new_guid = new_seat.seat_guid if new_seat else None

        if new_guid == current_guid:
            continue  # this position's seat answer didn't actually change

        if new_guid is None:
            logger.warning(
                f"order_modified: position {op.id} (order {order.code}) seat answer "
                f"'{raw_label}' no longer matches a known seat; keeping previous "
                f"assignment {current_guid!r} in place. Run simpleseating_audit to review."
            )
            continue

        holder = assigned_guids.get(new_guid)
        if holder and holder.order_position_id != op.id:
            logger.warning(
                f"order_modified: position {op.id} (order {order.code}) was edited to seat "
                f"'{raw_label}' ({new_guid}), but that seat is already assigned to position "
                f"{holder.order_position_id}. Leaving both assignments untouched; needs manual review."
            )
            continue

        if current:
            current.delete()
            assigned_guids.pop(current_guid, None)
        new_assignment, _ = SeatAssignment.objects.get_or_create(
            event=event, seat_guid=new_guid, defaults={'order_position_id': op.id}
        )
        assignments[op.id] = new_assignment
        assigned_guids[new_guid] = new_assignment
        logger.info(f"order_modified: position {op.id} (order {order.code}) seat re-synced to '{raw_label}' ({new_guid})")

        import json as json_module
        try:
            meta_dict = json_module.loads(op.meta_info) if isinstance(op.meta_info, str) else (op.meta_info or {})
            if not isinstance(meta_dict, dict):
                meta_dict = {}
        except (ValueError, TypeError):
            meta_dict = {}
        meta_dict['seat_number'] = new_seat.label
        op.meta_info = json_module.dumps(meta_dict)
        op.save(update_fields=['meta_info'])

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
