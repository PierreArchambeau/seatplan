"""
Checks shared by the `simpleseating_ticket_check` command and by the Celery
task it can run on a worker: the same code answers "does this environment
draw the seating plan?" wherever it runs.
"""
import logging


class _Collector(logging.Handler):
    """Keeps what the plugin logs while rendering, so the real error can be shown."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text += ' -- %s: %s' % (type(record.exc_info[1]).__name__, record.exc_info[1])
        self.messages.append(text)


LIBCAIRO_HINT = (
    'The Python package cairosvg needs the system library libcairo (Debian/Ubuntu: apt install libcairo2; '
    'Alpine: apk add cairo). It must be present in the environment that runs the Celery workers, and the '
    'workers must be restarted after installing it.'
)


def check_cairo():
    """Returns (ok, message) about cairosvg and libcairo in *this* process."""
    try:
        import cairosvg
        import cairocffi
    except ImportError as e:
        return False, ('cairosvg is not installed in this Python environment (%s). Install it with pip in the '
                       'same virtualenv as pretix.' % e)
    except OSError as e:
        return False, 'cairosvg could not load libcairo: %s. %s' % (e, LIBCAIRO_HINT)
    version = getattr(cairosvg, '__version__', '?')
    try:
        lib_version = cairocffi.cairo_version_string()
    except OSError as e:
        return False, 'libcairo could not be loaded: %s. %s' % (e, LIBCAIRO_HINT)
    try:
        png = cairosvg.svg2png(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>')
    except Exception as e:
        return False, 'cairosvg %s / libcairo %s are present but a test drawing failed: %s: %s' % (
            version, lib_version, type(e).__name__, e)
    return True, 'cairosvg %s, libcairo %s, test drawing produced %d bytes' % (version, lib_version, len(png))


def render_for_seat(event, cfg, seat_guid, seat_label):
    """Draws the plan for one seat. Returns (png bytes or None, messages logged while drawing)."""
    from .ticket_image import render_seat_plan_png

    collector = _Collector()
    logger = logging.getLogger('pretix_simpleseatingplan.ticket_image')
    logger.addHandler(collector)
    try:
        return render_seat_plan_png(event, cfg, seat_guid, seat_label), collector.messages
    finally:
        logger.removeHandler(collector)
