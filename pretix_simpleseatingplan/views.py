
import json
import re
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import JsonResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from pretix.base.models import CartPosition, Event, Item
from pretix.control.permissions import event_permission_required
from django.templatetags.static import static
from .forms import SeatingSettingsForm
from .models import SeatingConfig, Seat, SeatHold, SeatAssignment
from .audit import run_seat_audit
from .holds import claim_seat
from .svg_sanitize import SvgSanitizeError, clean_plan_svg, sanitize_svg

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SEATS = 100000


class PlanImportError(ValueError):
    """The uploaded plan file cannot be imported; the message is shown to the admin."""


def _get_event(organizer, event):
    try:
        return Event.objects.get(organizer__slug=organizer, slug=event)
    except Event.DoesNotExist:
        raise Http404()

def _purge_expired(event):
    SeatHold.objects.filter(event=event, expires__lte=timezone.now()).delete()

_COLOR_RE = re.compile(r'^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|(rgb|hsl)a?\([0-9.,%\s]{1,40}\))$')

def _safe_color(value, default):
    """Category colours come from the uploaded JSON: only accept plain CSS colours."""
    value = str(value or '').strip()
    return value if _COLOR_RE.match(value) else default

def _svg_from_seats_editor(layout, prefix):
    size = layout.get('size') or {}
    width = int(size.get('width', 900))
    height = int(size.get('height', 900))
    cat_colors = {}
    for c in (layout.get('categories') or []):
        name = c.get('name')
        if name:
            cat_colors[name] = _safe_color(c.get('color'), '#22c55e')
    def seat_color(seat_obj):
        cname = seat_obj.get('category')
        if cname and cname in cat_colors:
            return cat_colors[cname]
        if cat_colors:
            return list(cat_colors.values())[0]
        return '#22c55e'
    def esc(s) -> str:
        s = '' if s is None else str(s)
        return s.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n')
    out.append('  <rect x="0" y="0" width="100%" height="100%" fill="#f8f9fa"/>\n')
    for z in (layout.get('zones') or []):
        zx = float((z.get('position') or {}).get('x', 0))
        zy = float((z.get('position') or {}).get('y', 0))
        for row in (z.get('rows') or []):
            rx = float((row.get('position') or {}).get('x', 0))
            ry = float((row.get('position') or {}).get('y', 0))
            rlabel = str(row.get('row_label') or row.get('row_number') or '').strip()
            if rlabel:
                out.append(f'  <text class="row-label" x="{zx+rx}" y="{zy+ry-8}">{esc(rlabel)}</text>\n')
            for seat in (row.get('seats') or []):
                guid = seat.get('seat_guid')
                uuid = seat.get('uuid')
                if not guid:
                    continue
                sx = float((seat.get('position') or {}).get('x', 0))
                sy = float((seat.get('position') or {}).get('y', 0))
                x = zx + rx + sx
                y = zy + ry + sy
                fill = seat_color(seat)
                seat_id = f'{prefix}{guid}'
                sn = str(seat.get('seat_number') or '').strip()
                seat_label = (f'{rlabel}-{sn}').strip('-')
                seat_cat = (seat.get('category') or '').strip()
                if not seat_cat and (layout.get('categories') or []):
                    seat_cat = (layout.get('categories')[0].get('name') or '').strip()
                out.append(f'  <g id="{esc(seat_id)}" data-seat-id="{esc(uuid)}" data-seat-label="{esc(seat_label)}" data-seat-category="{esc(seat_cat)}">\n')
                radius = float(seat.get('radius', 12))
                out.append(f'    <circle class="seat-dot" cx="{x:.2f}" cy="{y:.2f}" r="{radius}" fill="{esc(fill)}" stroke="#0f172a" stroke-width="1"/>\n')
                if sn:
                    out.append(f'    <text x="{x:.2f}" y="{y+3:.2f}" text-anchor="middle" font-size="10" fill="#0f172a">{esc(sn)}</text>\n')
                out.append('  </g>\n')
    out.append('</svg>\n')
    return ''.join(out)

def _seats_from_layout(layout):
    """[(guid, label, category)] for every seat of a seats-editor layout."""
    seats = {}
    default_cat = ''
    if (layout.get('categories') or []):
        default_cat = (layout.get('categories')[0].get('name') or '').strip()
    for z in (layout.get('zones') or []):
        for row in (z.get('rows') or []):
            rlabel = str(row.get('row_label') or row.get('row_number') or '').strip()
            for seat in (row.get('seats') or []):
                guid = seat.get('seat_guid')
                if not guid:
                    continue
                guid = str(guid)
                sn = str(seat.get('seat_number') or '').strip()
                label = (f'{rlabel}-{sn}').strip('-') or guid
                seat_cat = (seat.get('category') or '').strip() or default_cat
                seats.setdefault(guid, (label, seat_cat))
    return [(g, l, c) for g, (l, c) in seats.items()]

def _seats_from_svg(svg, prefix):
    """[(guid, label, category)] for every element id starting with the prefix."""
    from lxml import etree
    root = etree.fromstring(svg.encode('utf-8'))
    guids = []
    for full_id in root.xpath('//@id'):
        if full_id.startswith(prefix) and len(full_id) > len(prefix):
            guids.append(full_id[len(prefix):])
    return [(g, g, '') for g in dict.fromkeys(guids)]

def _replace_seats(event, seats):
    """Replace the event's seats. The caller wraps this in a transaction so a
    failure cannot leave the event without seats."""
    if len(seats) > MAX_SEATS:
        raise PlanImportError('The plan has too many seats (maximum %d).' % MAX_SEATS)
    for guid, _label, _cat in seats:
        if len(guid) > 120:
            raise PlanImportError('Seat id "%s..." is too long (maximum 120 characters).' % guid[:20])
    Seat.objects.filter(event=event).delete()
    Seat.objects.bulk_create([
        Seat(event=event, seat_guid=g, label=l[:200], category=c[:200]) for g, l, c in seats
    ])
    return len(seats)

def _read_upload(f):
    if f.size > MAX_UPLOAD_BYTES:
        raise PlanImportError('The file is too large (maximum %d MB).' % (MAX_UPLOAD_BYTES // (1024 * 1024)))
    return f.read()

def _import_plan(event, cfg, json_file, svg_file):
    """Applies an uploaded plan to `cfg`/the event's seats and returns the
    number of imported seats. Raises PlanImportError / SvgSanitizeError for
    unusable files; the caller runs this inside a transaction."""
    if json_file:
        try:
            layout = json.loads(_read_upload(json_file).decode('utf-8', 'replace'))
        except (ValueError, RecursionError):
            raise PlanImportError('The JSON file is not valid.')
        if not isinstance(layout, dict):
            raise PlanImportError('The JSON file does not look like a seats.pretix.eu export.')
        try:
            svg = sanitize_svg(_svg_from_seats_editor(layout, cfg.seat_id_prefix))
            seats = _seats_from_layout(layout)
        except (AttributeError, TypeError, ValueError, KeyError):
            raise PlanImportError('The JSON file does not look like a seats.pretix.eu export.')
    elif svg_file:
        svg = sanitize_svg(_read_upload(svg_file))
        seats = _seats_from_svg(svg, cfg.seat_id_prefix)
    else:
        cfg.save()
        return Seat.objects.filter(event=event).count()
    count = _replace_seats(event, seats)
    cfg.svg = svg
    cfg.save()
    return count

@event_permission_required('can_change_settings')
def settings(request, organizer, event):
    ev = _get_event(organizer, event)
    cfg, _ = SeatingConfig.objects.get_or_create(event=ev)
    if request.method == 'POST':
        form = SeatingSettingsForm(request.POST, request.FILES, event=ev, instance=cfg)
        if form.is_valid():
            cfg = form.save(commit=False)
            try:
                with transaction.atomic():
                    imported = _import_plan(ev, cfg, request.FILES.get('json_file'), request.FILES.get('svg_file'))
            except (PlanImportError, SvgSanitizeError) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, 'Saved. Imported %d seats.' % imported)
                return redirect(request.path)
    else:
        initial = {}
        if cfg.item_id:
            try:
                initial['item'] = Item.objects.get(event=ev, id=cfg.item_id)
            except Item.DoesNotExist:
                pass
        form = SeatingSettingsForm(event=ev, instance=cfg, initial=initial)
    return render(request, 'pretix_simpleseatingplan/control/settings.html', {
        'event': ev, 'form': form, 'cfg': cfg, 'seat_count': Seat.objects.filter(event=ev).count(),
        # Sanitized on every render: plans stored before sanitization existed may be hostile.
        # Read from the DB, not from `cfg`, which may hold unsaved form input after a failed upload.
        'svg_preview': clean_plan_svg(SeatingConfig.objects.filter(event=ev).values_list('svg', flat=True).first()),
    })

@event_permission_required('can_change_settings')
@event_permission_required('can_view_orders')
def audit(request, organizer, event):
    # The audit exposes order codes and customer answers, so managing the
    # event settings alone is not enough; repairing assignments additionally
    # needs the right to change orders.
    if request.method == 'POST' and request.POST.get('action') == 'fix':
        if not request.user.has_event_permission(request.organizer, request.event, 'can_change_orders', request=request):
            raise PermissionDenied('You do not have permission to change orders.')
    ev = _get_event(organizer, event)
    try:
        cfg = SeatingConfig.objects.get(event=ev)
    except SeatingConfig.DoesNotExist:
        messages.error(request, 'Seating plan not configured for this event.')
        return redirect('plugins:pretix_simpleseatingplan:settings', organizer=organizer, event=event)

    result = None
    audit_plan = None
    if not cfg.item_id or not cfg.question_label_id:
        messages.warning(request, 'Seating plan is not fully configured yet (missing ticket item or seat question).')
    else:
        fix = request.method == 'POST' and request.POST.get('action') == 'fix'
        result = run_seat_audit(ev, cfg, fix=fix)
        if fix:
            fixed_count = sum(1 for r in result['resolved'] if r['fixed'])
            if fixed_count:
                messages.success(request, '%d missing seat assignment(s) fixed.' % fixed_count)
            else:
                messages.info(request, 'Nothing to fix.')

        if cfg.svg:
            _purge_expired(ev)
            # Sanitized (this also drops <style> blocks, which CSP would block anyway).
            clean_svg = clean_plan_svg(cfg.svg)
            audit_plan = {
                'svg': clean_svg,
                'prefix': cfg.seat_id_prefix,
                'sold': list(SeatAssignment.objects.filter(event=ev).values_list('seat_guid', flat=True)),
                'held': list(SeatHold.objects.filter(event=ev).values_list('seat_guid', flat=True)),
                'conflict': [c['seat_guid'] for c in result['conflicts']],
                'missing': [r['seat_guid'] for r in result['resolved']],
            }
    return render(request, 'pretix_simpleseatingplan/control/audit.html', {
        'event': ev, 'cfg': cfg, 'result': result, 'audit_plan': audit_plan,
    })

def plan_svg(request, organizer, event, **kwargs):
    ev = _get_event(organizer, event)
    try:
        cfg = SeatingConfig.objects.get(event=ev)
    except SeatingConfig.DoesNotExist:
        return HttpResponse('No config', content_type='text/plain', status=404)
    if not cfg.svg:
        return HttpResponse('No SVG in config', content_type='text/plain', status=404)
    svg = clean_plan_svg(cfg.svg)
    if not svg:
        return HttpResponse('No usable SVG in config', content_type='text/plain', status=404)
    response = HttpResponse(svg, content_type='image/svg+xml; charset=utf-8')
    response['Content-Disposition'] = 'inline; filename="plan.svg"'
    response['X-Content-Type-Options'] = 'nosniff'
    # Note: do not add a Content-Security-Policy header here. pretix already sets a
    # strict one on every response (no inline script) and its middleware crashes
    # on directives it does not know, such as "sandbox".
    return response

def status(request, organizer, event, **kwargs):
    # Verify user is in a valid checkout session for this event
    if not (hasattr(request, 'session') and request.session.session_key) and not request.user.is_authenticated:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    ev = _get_event(organizer, event)
    # Polled every second by every open checkout page: stay read-only (just
    # ignore expired holds) instead of running a DELETE per request. Actual
    # purging happens in hold/validate/the periodic task.
    sold = list(SeatAssignment.objects.filter(event=ev).values_list('seat_guid', flat=True))
    held = list(SeatHold.objects.filter(event=ev, expires__gt=timezone.now()).values_list('seat_guid', flat=True))
    return JsonResponse({'sold':sold,'held':held})

def _owns_cart_position(request, ev, cartpos_id):
    """True if `cartpos_id` is a cart position of this event that lives in one
    of the carts of the requester's own session.

    pretix keeps every cart the session owns as a key of session['carts']
    (see pretix.presale.views.cart.get_or_create_cart_id) and stamps each
    CartPosition with that cart id. cartpos_id itself comes straight from the
    browser, so without this check anybody could hold, steal or release seats
    on behalf of somebody else's cart.
    """
    if cartpos_id <= 0:
        return False
    cart_ids = list(request.session.get('carts', {}).keys())
    if not cart_ids:
        return False
    return CartPosition.objects.filter(pk=cartpos_id, event=ev, cart_id__in=cart_ids).exists()


def _parse_cartpos_id(raw):
    """Returns the cart position id as int, or None if missing/not a number."""
    try:
        return int((raw or '').strip())
    except (TypeError, ValueError, AttributeError):
        return None


@require_POST
def hold(request, organizer, event, **kwargs):
    # Verify user has a valid session (checkout context)
    if not (hasattr(request, 'session') and request.session.session_key) and not request.user.is_authenticated:
        return JsonResponse({'ok':False,'error':'unauthorized'}, status=403)
    ev = _get_event(organizer, event)
    _purge_expired(ev)
    seat_guid = request.POST.get('seat_guid', '').strip()
    if not seat_guid:
        return JsonResponse({'ok':False,'error':'missing_seat_guid'}, status=400)
    cartpos_id = _parse_cartpos_id(request.POST.get('cartpos_id'))
    if cartpos_id is None:
        return JsonResponse({'ok':False,'error':'invalid_cartpos_id'}, status=400)
    if not _owns_cart_position(request, ev, cartpos_id):
        return JsonResponse({'ok':False,'error':'forbidden'}, status=403)
    if not Seat.objects.filter(event=ev, seat_guid=seat_guid).exists():
        return JsonResponse({'ok':False,'error':'unknown_seat'}, status=400)
    if SeatAssignment.objects.filter(event=ev, seat_guid=seat_guid).exists():
        return JsonResponse({'ok':False,'error':'sold'}, status=409)
    try:
        hold_minutes = SeatingConfig.objects.get(event=ev).hold_minutes
    except SeatingConfig.DoesNotExist:
        return JsonResponse({'ok':False,'error':'no_config'}, status=404)
    expires = timezone.now() + timezone.timedelta(minutes=hold_minutes)
    with transaction.atomic():
        if not claim_seat(ev, seat_guid, cartpos_id, expires):
            # Somebody else's live hold (expired ones were purged above).
            return JsonResponse({'ok':False,'error':'held'}, status=409)
        # A cart position holds one seat at a time: this bounds how many
        # seats a single shopper can tie up. cartpos_id is a real, owned
        # CartPosition pk here, so it is unique across sessions.
        SeatHold.objects.filter(event=ev, cart_position_id=cartpos_id).exclude(seat_guid=seat_guid).delete()
    return JsonResponse({'ok':True,'expires':expires.isoformat()})

@require_POST
def release(request, organizer, event, **kwargs):
    # Verify user has a valid session (checkout context)
    if not (hasattr(request, 'session') and request.session.session_key) and not request.user.is_authenticated:
        return JsonResponse({'ok':False,'error':'unauthorized'}, status=403)
    ev = _get_event(organizer, event)
    seat_guid = request.POST.get('seat_guid', '').strip()
    cartpos_id_str = request.POST.get('cartpos_id', '').strip()

    if not seat_guid:
        return JsonResponse({'ok':False,'error':'missing_seat_guid'}, status=400)
    if not cartpos_id_str:
        return JsonResponse({'ok':False,'error':'missing_cartpos_id'}, status=400)

    cartpos_id = _parse_cartpos_id(cartpos_id_str)
    if cartpos_id is None:
        return JsonResponse({'ok':False,'error':'invalid_cartpos_id'}, status=400)
    if not _owns_cart_position(request, ev, cartpos_id):
        return JsonResponse({'ok':False,'error':'forbidden'}, status=403)

    # Only delete the hold belonging to this specific cart position (owned by this user)
    deleted, _ = SeatHold.objects.filter(
        event=ev,
        seat_guid=seat_guid,
        cart_position_id=cartpos_id
    ).delete()

    if deleted == 0:
        return JsonResponse({'ok':False,'error':'hold_not_found'}, status=404)

    return JsonResponse({'ok':True})

def config_js(request, organizer, event, **kwargs):
    import logging
    # Verify user has a valid session (checkout context)
    if not (hasattr(request, 'session') and request.session.session_key) and not request.user.is_authenticated:
        return HttpResponse('// Unauthorized', content_type='application/javascript', status=403)
    ev = _get_event(organizer, event)
    try:
        cfg = SeatingConfig.objects.get(event=ev)
    except SeatingConfig.DoesNotExist:
        return HttpResponse('// No config', content_type='application/javascript')
    if not cfg.svg or not cfg.question_label_id:
        return HttpResponse('// Incomplete config', content_type='application/javascript')
    from pretix.base.models import Question
    if not Question.objects.filter(event=ev, pk=cfg.question_label_id).exists():
        return HttpResponse('// Question not found', content_type='application/javascript')
    from pretix.multidomain.urlreverse import eventreverse
    # Sanitized on every request (this also drops <style> blocks, which CSP would
    # block anyway): plans stored before sanitization existed may be hostile.
    clean_svg = clean_plan_svg(cfg.svg)
    if not clean_svg:
        return HttpResponse('// Plan SVG unusable', content_type='application/javascript')
    data = {
        'svg': clean_svg,
        'prefix': cfg.seat_id_prefix,
        'status_url': eventreverse(ev,'plugins:pretix_simpleseatingplan:status'),
        'hold_url': eventreverse(ev,'plugins:pretix_simpleseatingplan:hold'),
        'release_url': eventreverse(ev,'plugins:pretix_simpleseatingplan:release'),
        'question_label_id': cfg.question_label_id,
    }
    logger = logging.getLogger(__name__)
    try:
        js_url = static('pretix_simpleseatingplan/frontend/seatpicker.js')
    except Exception as e:
        logger.warning(f"Failed to resolve seatpicker.js static URL: {e}")
        js_url = None
    if js_url:
        payload = 'window.SimpleSeatingPlanCfg = ' + json.dumps(data) + ';' \
                  '(function(){try{var s=document.createElement("script");s.src=' + json.dumps(js_url) + ';s.defer=true;document.head.appendChild(s);}catch(e){console.error("Seatpicker loader error",e);}})();\n'
    else:
        payload = 'window.SimpleSeatingPlanCfg = ' + json.dumps(data) + ';\n' \
                  'console.warn("Seatpicker.js failed to load from static files. Check collectstatic and STATIC_ROOT configuration.");\n'
    return HttpResponse(payload, content_type='application/javascript')
