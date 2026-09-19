"""
Renders the configured seating plan SVG as a PNG with the purchased seat
highlighted, for use as a dynamic ticket image via pretix's
``layout_image_variables`` signal (see signals.py).
"""
import colorsys
import logging
import re

from lxml import etree

from .models import Seat, SeatAssignment
from .seat_matching import build_label_index, match_seat_by_label
from .svg_sanitize import clean_plan_svg

logger = logging.getLogger(__name__)

NS_SVG = 'http://www.w3.org/2000/svg'
# The purchased seat is drawn in a saturated colour on a plan whose OTHER seats are faded, so it stands out
# whatever colours the plan uses, in colour and when the ticket is printed in black and white. Red is the
# standard; if the seat is itself reddish (a plan with red seats), blue is used instead.
HIGHLIGHT_FILL = '#ef4444'
HIGHLIGHT_STROKE = '#7f1d1d'
HALO_STROKE = '#ef4444'
HIGHLIGHT_FILL_ALT = '#2563eb'
HIGHLIGHT_STROKE_ALT = '#1e3a8a'
HALO_STROKE_ALT = '#2563eb'
FADE_OPACITY = '0.3'
SHAPE_TAGS = {'circle', 'ellipse', 'rect', 'path', 'polygon', 'polyline', 'line'}

_NAMED_REDS = {'red', 'crimson', 'firebrick', 'darkred', 'tomato', 'orangered', 'indianred', 'maroon', 'brown'}
_HEX_RE = re.compile(r'^#([0-9a-f]{3}|[0-9a-f]{6})$', re.IGNORECASE)
_RGB_RE = re.compile(r'^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)', re.IGNORECASE)


def _is_reddish(colour):
    """True for colours a red highlight would not stand out against."""
    colour = (colour or '').strip().lower()
    if colour in _NAMED_REDS:
        return True
    m = _HEX_RE.match(colour)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    else:
        m = _RGB_RE.match(colour)
        if not m:
            return False
        r, g, b = (min(255, int(v)) for v in m.groups())
    hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return (hue <= 0.04 or hue >= 0.94) and saturation >= 0.35 and value >= 0.3


def _shape_fill(shape):
    """The fill a shape declares, as attribute or inline style (None if it does not)."""
    fill = shape.get('fill')
    if not fill:
        m = re.search(r'(?:^|;)\s*fill\s*:\s*([^;]+)', shape.get('style') or '')
        fill = m.group(1).strip() if m else None
    return fill


def resolve_seat_for_position(event, cfg, position):
    """
    Returns (seat_guid, seat_label) for an order position, or (None, None)
    if no seat can be determined. Prefers the SeatAssignment created at
    order_placed time (the authoritative source, and the only one that
    directly gives us a seat_guid to locate the seat in the SVG); falls
    back to matching the "Seat" question answer for the rare case where
    this runs before that assignment exists (e.g. PDF preview on an
    unconfirmed order).
    """
    assignment = SeatAssignment.objects.filter(event=event, order_position_id=position.id).first()
    if assignment:
        seat = Seat.objects.filter(event=event, seat_guid=assignment.seat_guid).first()
        return assignment.seat_guid, (seat.label if seat else None)

    if cfg.question_label_id:
        ans = position.answers.filter(question_id=cfg.question_label_id).first()
        if ans and ans.answer and ans.answer.strip():
            label_index = build_label_index(event)
            seat = match_seat_by_label(label_index, ans.answer)
            if seat:
                return seat.seat_guid, seat.label

    return None, None


def _find_seat_element(root, prefix, seat_guid):
    target_id = f'{prefix}{seat_guid}'
    matches = root.xpath(".//*[@id=$tid]", tid=target_id)
    if matches:
        return matches[0]
    matches = root.xpath(".//*[@data-seat-id=$sg]", sg=seat_guid)
    if matches:
        return matches[0]
    return None


def _highlight_element(el):
    """
    Recolors the matched seat shape(s) in place, in a saturated colour that
    contrasts with the seat's own colour (see HIGHLIGHT_FILL). For
    circle/ellipse seats (the shape our own plan editor generates) it also
    adds a dashed halo ring around them, so the seat is still identifiable by
    shape alone on a black & white print, not just by color.
    """
    shapes = [el] if etree.QName(el).localname in SHAPE_TAGS else [
        d for d in el.iter() if etree.QName(d).localname in SHAPE_TAGS
    ]
    if not shapes:
        return False

    original = next((f for f in (_shape_fill(sh) for sh in shapes) if f and f.lower() != 'none'), None)
    if _is_reddish(original):
        fill, stroke, halo_stroke = HIGHLIGHT_FILL_ALT, HIGHLIGHT_STROKE_ALT, HALO_STROKE_ALT
    else:
        fill, stroke, halo_stroke = HIGHLIGHT_FILL, HIGHLIGHT_STROKE, HALO_STROKE

    for shape in shapes:
        tag = etree.QName(shape).localname
        shape.attrib.pop('style', None)
        shape.set('fill', fill)
        shape.set('stroke', stroke)
        shape.set('stroke-width', '3')

        halo = None
        if tag == 'circle':
            try:
                cx, cy, r = float(shape.get('cx', 0)), float(shape.get('cy', 0)), float(shape.get('r', 10))
            except ValueError:
                cx = cy = r = None
            if r is not None:
                halo = etree.Element(f'{{{NS_SVG}}}circle')
                halo.set('cx', str(cx))
                halo.set('cy', str(cy))
                halo.set('r', str(r * 2.2))
        elif tag == 'ellipse':
            try:
                cx = float(shape.get('cx', 0))
                cy = float(shape.get('cy', 0))
                rx = float(shape.get('rx', 10))
                ry = float(shape.get('ry', 10))
            except ValueError:
                cx = cy = rx = ry = None
            if rx is not None:
                halo = etree.Element(f'{{{NS_SVG}}}ellipse')
                halo.set('cx', str(cx))
                halo.set('cy', str(cy))
                halo.set('rx', str(rx * 2.2))
                halo.set('ry', str(ry * 2.2))

        if halo is not None:
            halo.set('fill', 'none')
            halo.set('stroke', halo_stroke)
            halo.set('stroke-width', '3')
            halo.set('stroke-dasharray', '4,3')
            shape.addprevious(halo)

    return True


def _fade_other_seats(root, prefix, target):
    """Washes out every seat except `target`, so the purchased seat is the only
    saturated thing on the plan. Everything that is not a seat (stage, labels,
    sections) is left alone."""
    for node in root.iter():
        if node is target or not isinstance(node.tag, str):
            continue
        node_id = node.get('id') or ''
        is_seat = (prefix and node_id.startswith(prefix)) or node.get('data-seat-id') is not None
        if not is_seat:
            continue
        # never fade something that contains the target (a seat nested in a bigger element)
        if target is not None and target in node.iterdescendants():
            continue
        node.set('opacity', FADE_OPACITY)


def _canvas_size(root):
    view_box = root.get('viewBox')
    if view_box:
        parts = view_box.replace(',', ' ').split()
        if len(parts) == 4:
            try:
                return float(parts[2]), float(parts[3])
            except ValueError:
                pass
    try:
        return float(root.get('width', 900)), float(root.get('height', 900))
    except ValueError:
        return 900.0, 900.0


def _add_legend(root, seat_label):
    width, height = _canvas_size(root)
    pad = max(width, height) * 0.02
    font_size = max(width, height) * 0.035
    box_w = font_size * (len(seat_label) + 4) * 0.62
    box_h = font_size * 1.9

    bg = etree.SubElement(root, f'{{{NS_SVG}}}rect')
    bg.set('x', str(pad))
    bg.set('y', str(height - pad - box_h))
    bg.set('width', str(box_w))
    bg.set('height', str(box_h))
    bg.set('rx', str(font_size * 0.3))
    bg.set('fill', '#0f172a')
    bg.set('fill-opacity', '0.85')

    text = etree.SubElement(root, f'{{{NS_SVG}}}text')
    text.set('x', str(pad + font_size * 0.5))
    text.set('y', str(height - pad - box_h * 0.32))
    text.set('font-size', str(font_size))
    text.set('font-weight', 'bold')
    text.set('fill', '#ffffff')
    text.text = seat_label


def render_seat_plan_png(event, cfg, seat_guid, seat_label, output_width=1600):
    """Returns PNG bytes with the given seat highlighted, or None if the
    plan SVG is missing/unparseable or the seat can't be located in it."""
    # The stored plan is organizer-supplied and may pre-date sanitization; this
    # also guarantees cairosvg never sees external references (file://, http://)
    # or entity declarations.
    svg = clean_plan_svg(cfg.svg)
    if not svg:
        return None
    try:
        root = etree.fromstring(svg.encode('utf-8'))
    except etree.XMLSyntaxError:
        logger.warning("simpleseatingplan: could not parse plan SVG for event %s", event.slug)
        return None

    # Presale strips <style> blocks before showing the SVG too -- our highlight
    # uses presentation attributes directly and shouldn't be overridden by CSS.
    for style_el in root.xpath(".//*[local-name()='style']"):
        style_el.getparent().remove(style_el)

    el = _find_seat_element(root, cfg.seat_id_prefix or '', seat_guid)
    if el is None:
        logger.warning(
            "simpleseatingplan: seat '%s' (id '%s%s' or data-seat-id) not found in plan SVG for event %s -- "
            "ticket image will be skipped for this seat",
            seat_guid, cfg.seat_id_prefix or '', seat_guid, event.slug,
        )
        return None
    _fade_other_seats(root, cfg.seat_id_prefix or '', el)
    if not _highlight_element(el):
        logger.warning(
            "simpleseatingplan: seat element '%s' found in plan SVG for event %s but contains no recognizable "
            "shape (circle/ellipse/rect/path/polygon/polyline/line) to highlight",
            seat_guid, event.slug,
        )
        return None

    if seat_label:
        _add_legend(root, seat_label)

    try:
        import cairosvg
        return cairosvg.svg2png(bytestring=etree.tostring(root), output_width=output_width, background_color='white')
    except Exception:
        logger.exception("simpleseatingplan: failed to render seat plan PNG for event %s", event.slug)
        return None
