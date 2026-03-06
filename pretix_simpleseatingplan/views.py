
import json
import re
from html import unescape
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import JsonResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from pretix.base.models import Event, Item
from pretix.control.permissions import event_permission_required
from django.templatetags.static import static
from .forms import SeatingSettingsForm
from .models import SeatingConfig, Seat, SeatHold, SeatAssignment


def _get_event(organizer, event):
    try:
        return Event.objects.get(organizer__slug=organizer, slug=event)
    except Event.DoesNotExist:
        raise Http404()

def _purge_expired(event):
    SeatHold.objects.filter(event=event, expires__lte=timezone.now()).delete()

def _svg_from_seats_editor(layout, prefix):
    size = layout.get('size') or {}
    width = int(size.get('width', 900))
    height = int(size.get('height', 900))
    cat_colors = {}
    for c in (layout.get('categories') or []):
        name = c.get('name')
        if name:
            cat_colors[name] = c.get('color') or '#22c55e'
    def seat_color(seat_obj):
        cname = seat_obj.get('category')
        if cname and cname in cat_colors:
            return cat_colors[cname]
        if cat_colors:
            return list(cat_colors.values())[0]
        return '#22c55e'
    def esc(s: str) -> str:
        return s.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n')
    out.append('  <rect x="0" y="0" width="100%" height="100%" fill="#f8f9fa"/>\n')
    out.append('  <defs><style><![CDATA[\n')
    out.append('    .seat-dot { stroke: #0f172a; stroke-width: 1; }\n')
    out.append('    .row-label { fill: #222; font: 12px sans-serif; }\n')
    out.append('  ]]></style></defs>\n')
    for z in (layout.get('zones') or []):
        zx = float((z.get('position') or {}).get('x', 0))
        zy = float((z.get('position') or {}).get('y', 0))
        for row in (z.get('rows') or []):
            rx = float((row.get('position') or {}).get('x', 0))
            ry = float((row.get('position') or {}).get('y', 0))
            rlabel = (row.get('row_label') or row.get('row_number') or '').strip()
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
                out.append(f'    <circle class="seat-dot" cx="{x:.2f}" cy="{y:.2f}" r="{seat.get("radius", 12)}" fill="{fill}"/>\n')
                if sn:
                    out.append(f'    <text x="{x:.2f}" y="{y+3:.2f}" text-anchor="middle" font-size="10" fill="#0f172a">{esc(sn)}</text>\n')
                out.append('  </g>\n')
    out.append('</svg>\n')
    return ''.join(out)

def _import_seats_from_layout(event, layout):
    Seat.objects.filter(event=event).delete()
    count = 0
    default_cat = ''
    if (layout.get('categories') or []):
        default_cat = (layout.get('categories')[0].get('name') or '').strip()
    for z in (layout.get('zones') or []):
        for row in (z.get('rows') or []):
            rlabel = (row.get('row_label') or row.get('row_number') or '').strip()
            for seat in (row.get('seats') or []):
                guid = seat.get('seat_guid')
                if not guid:
                    continue
                sn = str(seat.get('seat_number') or '').strip()
                label = (f'{rlabel}-{sn}').strip('-') or str(guid)
                seat_cat = (seat.get('category') or '').strip() or default_cat
                Seat.objects.create(event=event, seat_guid=guid, label=label, category=seat_cat)
                count += 1
    return count

@event_permission_required('can_change_settings')
def settings(request, organizer, event):
    ev = _get_event(organizer, event)
    cfg, _ = SeatingConfig.objects.get_or_create(event=ev)
    if request.method == 'POST':
        form = SeatingSettingsForm(request.POST, request.FILES, event=ev, instance=cfg)
        if form.is_valid():
            cfg = form.save(commit=False)
            jf = request.FILES.get('json_file')
            sf = request.FILES.get('svg_file')
            imported = None
            if jf:
                raw = jf.read().decode('utf-8','replace')
                layout = json.loads(raw)
                cfg.svg = _svg_from_seats_editor(layout, cfg.seat_id_prefix)
                cfg.save()
                imported = _import_seats_from_layout(ev, layout)
            elif sf:
                raw = sf.read().decode('utf-8','replace')
                cfg.svg = unescape(raw)
                cfg.save()
                prefix = re.escape(cfg.seat_id_prefix)
                pattern = rf"\bid\s*=\s*([\"'])({prefix}[^\"']+)\1"
                ids = {m.group(2) for m in re.finditer(pattern, cfg.svg)}
                Seat.objects.filter(event=ev).delete()
                for full_id in ids:
                    guid = full_id[len(cfg.seat_id_prefix):]
                    Seat.objects.create(event=ev, seat_guid=guid, label=guid, category='')
                imported = Seat.objects.filter(event=ev).count()
            else:
                cfg.save(); imported = Seat.objects.filter(event=ev).count()
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
    return render(request, 'pretix_simpleseatingplan/control/settings.html', {'event':ev,'form':form,'cfg':cfg,'seat_count':Seat.objects.filter(event=ev).count()})

def plan_svg(request, organizer, event, **kwargs):
    ev = _get_event(organizer, event)
    try:
        cfg = SeatingConfig.objects.get(event=ev)
    except SeatingConfig.DoesNotExist:
        return HttpResponse('No config', content_type='text/plain', status=404)
    if not cfg.svg:
        return HttpResponse('No SVG in config', content_type='text/plain', status=404)
    # Set Content-Disposition to prevent XSS if SVG contains script tags
    response = HttpResponse(cfg.svg, content_type='image/svg+xml; charset=utf-8')
    response['Content-Disposition'] = 'inline; filename="plan.svg"'
    response['X-Content-Type-Options'] = 'nosniff'
    return response

def status(request, organizer, event, **kwargs):
    # Verify user is in a valid checkout session for this event
    if not (hasattr(request, 'session') and request.session.session_key) and not request.user.is_authenticated:
        return JsonResponse({'error': 'unauthorized'}, status=403)
    ev = _get_event(organizer, event)
    _purge_expired(ev)
    sold = list(SeatAssignment.objects.filter(event=ev).values_list('seat_guid', flat=True))
    held = list(SeatHold.objects.filter(event=ev).values_list('seat_guid', flat=True))
    return JsonResponse({'sold':sold,'held':held})

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
    try:
        cartpos_id = int(request.POST.get('cartpos_id', 0) or 0)
    except (TypeError, ValueError):
        cartpos_id = 0
    if not Seat.objects.filter(event=ev, seat_guid=seat_guid).exists():
        return JsonResponse({'ok':False,'error':'unknown_seat'}, status=400)
    if SeatAssignment.objects.filter(event=ev, seat_guid=seat_guid).exists():
        return JsonResponse({'ok':False,'error':'sold'}, status=409)
    # Remove existing hold for this same seat (re-selection by anyone)
    SeatHold.objects.filter(event=ev, seat_guid=seat_guid).delete()
    # NOTE: we no longer delete by cart_position_id because form indices
    # are NOT unique across browser sessions, causing cross-session
    # hold deletion.  Old holds are released explicitly by the JS
    # via the release endpoint when the user changes their seat.
    expires = timezone.now() + timezone.timedelta(minutes=SeatingConfig.objects.get(event=ev).hold_minutes)
    try:
        with transaction.atomic():
            SeatHold.objects.create(event=ev, seat_guid=seat_guid, cart_position_id=cartpos_id, expires=expires)
    except IntegrityError:
        return JsonResponse({'ok':False,'error':'held'}, status=409)
    return JsonResponse({'ok':True,'expires':expires.isoformat()})

@require_POST
def release(request, organizer, event, **kwargs):
    # Verify user has a valid session (checkout context)
    if not (hasattr(request, 'session') and request.session.session_key) and not request.user.is_authenticated:
        return JsonResponse({'ok':False,'error':'unauthorized'}, status=403)
    ev = _get_event(organizer, event)
    seat_guid = request.POST.get('seat_guid', '').strip()
    if not seat_guid:
        return JsonResponse({'ok':False,'error':'missing_seat_guid'}, status=400)
    SeatHold.objects.filter(event=ev, seat_guid=seat_guid).delete()
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
    from pretix.multidomain.urlreverse import eventreverse
    data = {
        'svg': cfg.svg,
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
