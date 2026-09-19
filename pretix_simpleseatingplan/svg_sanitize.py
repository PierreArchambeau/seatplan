"""
Allow-list sanitizer for the seating plan SVG.

The plan is uploaded (or generated from a JSON export) by an event organizer,
but it is then injected as live markup into pages of *other* people: the
customers' checkout page, and the control panel of every other team member.
Nothing in it should be able to run script, load a remote resource or
overlay a form on the page, so instead of trying to strip known-bad things
we rebuild the document from scratch and keep only what a seating plan
legitimately needs: shapes, text, gradients and fragment references.

Everything not on the allow-lists below -- <script>, <foreignObject>,
<style>, <a>, animations, filters, event-handler attributes, external
hrefs, url() pointing outside the document, DOCTYPE entities (XXE / entity
expansion) -- is dropped.
"""
import logging
import re
from functools import lru_cache

from lxml import etree

logger = logging.getLogger(__name__)

SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_NS = 'http://www.w3.org/1999/xlink'
XML_NS = 'http://www.w3.org/XML/1998/namespace'

MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_ELEMENTS = 200_000

ALLOWED_ELEMENTS = frozenset({
    'svg', 'g', 'defs', 'symbol', 'use', 'title', 'desc',
    'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'text', 'tspan', 'textPath',
    'linearGradient', 'radialGradient', 'stop', 'clipPath', 'mask', 'pattern',
    'image',
})
# Elements whose character data is meaningful (and harmless: it is escaped
# on output). Whitespace between other elements is dropped.
TEXT_ELEMENTS = frozenset({'text', 'tspan', 'textPath', 'title', 'desc'})
# Elements that are dropped but whose (sanitized) children are kept.
UNWRAP_ELEMENTS = frozenset({'a', 'switch'})

ALLOWED_ATTRIBUTES = frozenset({
    'id', 'class', 'style', 'transform', 'role',
    # geometry
    'x', 'y', 'dx', 'dy', 'width', 'height', 'cx', 'cy', 'r', 'rx', 'ry',
    'x1', 'y1', 'x2', 'y2', 'fx', 'fy', 'points', 'd', 'pathLength',
    'viewBox', 'preserveAspectRatio', 'version', 'baseProfile',
    # painting
    'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity',
    'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit', 'stroke-dasharray',
    'stroke-dashoffset', 'opacity', 'visibility', 'display', 'color',
    'clip-path', 'clip-rule', 'mask', 'pointer-events',
    # text
    'font-family', 'font-size', 'font-weight', 'font-style', 'text-anchor',
    'text-decoration', 'dominant-baseline', 'alignment-baseline',
    'letter-spacing', 'word-spacing', 'textLength', 'lengthAdjust', 'rotate',
    'startOffset',
    # gradients / patterns / masks
    'offset', 'stop-color', 'stop-opacity', 'gradientUnits', 'gradientTransform',
    'spreadMethod', 'patternUnits', 'patternContentUnits', 'patternTransform',
    'maskUnits', 'maskContentUnits', 'clipPathUnits',
})
ALLOWED_ATTRIBUTE_PREFIXES = ('data-', 'aria-')

_HREF_KEYS = {'href', f'{{{XLINK_NS}}}href'}
_DATA_IMAGE_RE = re.compile(r'^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]*$', re.IGNORECASE)
_FRAGMENT_RE = re.compile(r'^#[^\s]+$')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f\s]+')
# Anything that could reach out of the document or run script inside a value.
_DANGEROUS_VALUE_RE = re.compile(r'javascript:|vbscript:|data:|expression\(|@import|behavior\s*:|-moz-binding', re.IGNORECASE)
_EXTERNAL_URL_RE = re.compile(r'url\(\s*[\'"]?\s*(?!#)', re.IGNORECASE)
_ENTITY_DECL_RE = re.compile(rb'<!ENTITY', re.IGNORECASE)


class SvgSanitizeError(ValueError):
    """The uploaded plan is not a usable SVG."""


def _value_is_safe(value):
    squashed = _CONTROL_CHARS_RE.sub('', value)
    return not _DANGEROUS_VALUE_RE.search(squashed) and not _EXTERNAL_URL_RE.search(value)


def _clean_style(value):
    """Keep only the harmless declarations of an inline style attribute."""
    kept = []
    for decl in value.split(';'):
        if ':' not in decl:
            continue
        if _value_is_safe(decl):
            kept.append(decl.strip())
    return '; '.join(kept)


def _local_name(el):
    if not isinstance(el.tag, str):  # comments, processing instructions
        return None
    qname = etree.QName(el)
    if qname.namespace not in (None, SVG_NS):
        return None
    return qname.localname


def _clean_attributes(local, src, dst):
    for key, value in src.attrib.items():
        if key in _HREF_KEYS:
            value = value.strip()
            if local in ('use', 'textPath', 'linearGradient', 'radialGradient', 'pattern') and _FRAGMENT_RE.match(value):
                dst.set(f'{{{XLINK_NS}}}href' if key != 'href' else 'href', value)
            elif local == 'image' and _DATA_IMAGE_RE.match(value):
                dst.set('href' if key == 'href' else f'{{{XLINK_NS}}}href', value)
            continue
        if key == f'{{{XML_NS}}}space':
            if value in ('default', 'preserve'):
                dst.set(key, value)
            continue
        if key.startswith('{'):  # any other namespaced attribute
            continue
        if key not in ALLOWED_ATTRIBUTES and not key.startswith(ALLOWED_ATTRIBUTE_PREFIXES):
            continue
        if key == 'style':
            value = _clean_style(value)
            if not value:
                continue
        elif not _value_is_safe(value):
            continue
        dst.set(key, value)


def _rebuild(src, parent, counter):
    """Copy `src` (recursively) under `parent`, keeping only allow-listed
    nodes. Returns the new element, or None if `src` was dropped/unwrapped."""
    local = _local_name(src)
    if local is None:
        return None
    counter[0] += 1
    if counter[0] > MAX_ELEMENTS:
        raise SvgSanitizeError('The SVG contains too many elements.')

    if local in UNWRAP_ELEMENTS:
        for child in src:
            _rebuild(child, parent, counter)
        return None
    if local not in ALLOWED_ELEMENTS:
        return None

    dst = etree.SubElement(parent, f'{{{SVG_NS}}}{local}')
    _clean_attributes(local, src, dst)
    keep_text = local in TEXT_ELEMENTS
    if keep_text and src.text:
        dst.text = src.text
    for child in src:
        new_child = _rebuild(child, dst, counter)
        if keep_text and new_child is not None and child.tail:
            new_child.tail = child.tail
    return dst


def sanitize_svg(svg_text):
    """Returns a cleaned, serialized SVG document (no XML declaration), or
    raises SvgSanitizeError if the input is not a usable SVG."""
    if isinstance(svg_text, str):
        data = svg_text.encode('utf-8')
    else:
        data = bytes(svg_text)
    if len(data) > MAX_SVG_BYTES:
        raise SvgSanitizeError('The SVG file is too large.')
    if _ENTITY_DECL_RE.search(data):
        raise SvgSanitizeError('SVG files declaring XML entities are not supported.')

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
        huge_tree=False, remove_comments=True, remove_pis=True, recover=True,
    )
    try:
        src_root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise SvgSanitizeError('The file is not a valid SVG document.') from e
    if src_root is None or _local_name(src_root) != 'svg':
        raise SvgSanitizeError('The file is not an SVG document.')

    out_root = etree.Element(f'{{{SVG_NS}}}svg', nsmap={None: SVG_NS, 'xlink': XLINK_NS})
    _clean_attributes('svg', src_root, out_root)
    counter = [0]
    for child in src_root:
        _rebuild(child, out_root, counter)
    return etree.tostring(out_root, encoding='unicode')


@lru_cache(maxsize=32)
def _clean_cached(svg_text):
    try:
        return sanitize_svg(svg_text)
    except SvgSanitizeError as e:
        logger.warning('simpleseatingplan: stored plan SVG rejected by sanitizer: %s', e)
        return ''


def clean_plan_svg(svg_text):
    """Sanitized version of a *stored* plan, for use wherever it is rendered.

    Plans saved before sanitization existed may still contain hostile markup
    in the database, so every output path goes through this, not only the
    upload. Returns '' if the stored value cannot be sanitized. Cached
    because it runs on every shopper page view."""
    if not svg_text:
        return ''
    return _clean_cached(svg_text)
