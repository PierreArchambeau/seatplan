"""
Shared logic to match a manually-typed (or picker-filled) seat label to a
known Seat, tolerating common formatting differences (case, spaces, dashes,
leading zeros) instead of requiring an exact byte-for-byte match.
"""
import re

from pretix.base.models import Question

from .models import Seat

_SEP_RE = re.compile(r'[\s\-_./]+')
_LEADING_ZERO_RE = re.compile(r'(?<!\d)0+(\d)')


def normalize_seat_label(label):
    """Canonicalize a seat label for comparison: uppercase, strip separators
    (spaces, dashes, underscores, dots, slashes) and leading zeros in digit
    runs. E.g. "a - 012" and "A012" and "A12" all normalize to "A12"."""
    if not label:
        return ''
    s = _SEP_RE.sub('', label.strip().upper())
    return _LEADING_ZERO_RE.sub(r'\1', s)


def build_label_index(event):
    """Map normalized label -> Seat for every seat of the event. If two
    seats normalize to the same value (shouldn't normally happen), the
    first one found wins."""
    index = {}
    for seat in Seat.objects.filter(event=event):
        norm = normalize_seat_label(seat.label)
        if norm:
            index.setdefault(norm, seat)
    return index


def match_seat_by_label(label_index, raw_label):
    """Look up a Seat for a raw, possibly loosely-formatted label."""
    if not raw_label:
        return None
    return label_index.get(normalize_seat_label(raw_label))


def find_misplaced_seat_answer(position, cfg, label_index):
    """Detect a seat number typed into the wrong field: look at every
    answer on this position OTHER than the seat-label question and return
    the first one whose text matches a real seat, e.g. a "Nom"/"Prénom"
    field containing "A-12". Only free-text questions are considered, to
    avoid false positives on numeric/choice/boolean questions. Returns the
    QuestionAnswer if found, else None."""
    for ans in position.answers.all():
        if ans.question_id == cfg.question_label_id or not ans.answer:
            continue
        qtype = getattr(ans.question, 'type', None)
        if qtype not in (Question.TYPE_STRING, Question.TYPE_TEXT):
            continue
        if match_seat_by_label(label_index, ans.answer):
            return ans
    return None
