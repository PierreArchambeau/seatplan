"""
Shared fixtures for the backend (Python) regression tests.

These run against the real pretix installation with pretix's own test
settings (in-memory database, no migrations), driven through Django's test
client so that routing, middleware, sessions and the cart machinery are the
production ones -- see tests/backend/README.md for how to run them.
"""
import datetime
import time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone
from django_scopes import scopes_disabled
from pretix.base.models import CartPosition, Event, Item, Organizer, Question, Quota, Team, User

from pretix_simpleseatingplan.forms import Q_SEAT_LABEL
from pretix_simpleseatingplan.models import Seat, SeatingConfig

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">'
    '<g id="seat-a1" data-seat-label="A-1"><circle cx="50" cy="50" r="12" fill="#22c55e"/></g>'
    '<g id="seat-a2" data-seat-label="A-2"><circle cx="90" cy="50" r="12" fill="#22c55e"/></g>'
    '<g id="seat-a3" data-seat-label="A-3"><circle cx="130" cy="50" r="12" fill="#22c55e"/></g>'
    '</svg>'
)


class SeatingTestCase(TestCase):
    """An event with the plugin enabled, one ticket item, a configured plan
    with three seats (a1..a3), and helpers to act as independent shoppers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # pretix models are organizer-scoped; the tests query across
        # organizers/events freely, so disable scoping for the whole class.
        cls._scopes = scopes_disabled()
        cls._scopes.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._scopes.__exit__(None, None, None)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.organizer = Organizer.objects.create(name='Org', slug='org')
        self.event = Event.objects.create(
            organizer=self.organizer, name='Concert', slug='concert',
            date_from=timezone.now() + datetime.timedelta(days=30), live=True,
            plugins='pretix_simpleseatingplan',
        )
        self.item = Item.objects.create(event=self.event, name='Ticket', default_price=10, active=True)
        # Without a quota pretix refuses to put the item in a cart.
        self.quota = Quota.objects.create(event=self.event, name='All', size=100)
        self.quota.items.add(self.item)
        self.question = Question.objects.create(
            event=self.event, identifier=Q_SEAT_LABEL, question={'en': 'Seat'},
            type=Question.TYPE_STRING, required=True,
        )
        self.question.items.add(self.item)
        self.cfg = SeatingConfig.objects.create(
            event=self.event, item_id=self.item.id, question_label_id=self.question.id,
            svg=SVG, hold_minutes=10,
        )
        for guid, label in (('a1', 'A-1'), ('a2', 'A-2'), ('a3', 'A-3')):
            Seat.objects.create(event=self.event, seat_guid=guid, label=label, category='')

    # -- URLs ---------------------------------------------------------------
    def url(self, name):
        return '/%s/%s/_simpleseatingplan/%s' % (self.organizer.slug, self.event.slug, name)

    # -- shoppers -----------------------------------------------------------
    def new_shopper(self, positions=1):
        """A fresh browser: own cookie jar, own pretix cart with `positions`
        seat tickets. Returns (client, [cart position ids])."""
        client = Client()
        resp = client.post(
            '/%s/%s/cart/add' % (self.organizer.slug, self.event.slug),
            {'item_%d' % self.item.id: str(positions)},
        )
        assert resp.status_code in (200, 302), resp.status_code
        ids = list(
            CartPosition.objects.filter(
                event=self.event, cart_id__in=list(client.session.get('carts', {}).keys())
            ).order_by('id').values_list('id', flat=True)
        )
        assert len(ids) == positions, (ids, positions)
        return client, ids

    def bare_visitor(self):
        """A browser that has a session but no cart at all (e.g. an attacker
        that only loaded a shop page)."""
        client = Client()
        client.get('/%s/%s/' % (self.organizer.slug, self.event.slug))
        assert client.session.session_key
        return client

    def hold(self, client, seat_guid, cartpos_id):
        return client.post(self.url('hold/'), {'seat_guid': seat_guid, 'cartpos_id': str(cartpos_id)})

    def release(self, client, seat_guid, cartpos_id):
        return client.post(self.url('release/'), {'seat_guid': seat_guid, 'cartpos_id': str(cartpos_id)})

    # -- control panel ------------------------------------------------------
    def admin_client(self, email='admin@example.org', permissions=None):
        """A logged-in control panel user. `permissions=None` gives every
        event permission; otherwise a list of pretix permission names such as
        ['event.settings.general:write'] to build a restricted team member."""
        user = User.objects.create_user(email, 'secret-pw')
        if permissions is None:
            team = Team.objects.create(organizer=self.organizer, name='Admins', all_events=True, all_event_permissions=True)
        else:
            team = Team.objects.create(
                organizer=self.organizer, name='Limited-' + email, all_events=True,
                limit_event_permissions={p: True for p in permissions},
            )
        team.members.add(user)
        client = Client()
        assert client.login(email=email, password='secret-pw')
        session = client.session
        session['pretix_auth_login_time'] = int(time.time())
        session['pretix_auth_long_session'] = False
        session.save()
        return client

    def control_url(self, name=''):
        return '/control/event/%s/%s/simpleseatingplan/%s' % (self.organizer.slug, self.event.slug, name)

    def upload_plan(self, client, svg=None, json_text=None, **fields):
        """POST the settings form with a plan file, the way the browser does."""
        data = {'item': self.item.id, 'seat_id_prefix': 'seat-', 'hold_minutes': 10, 'category_variation_map': ''}
        data.update(fields)
        if svg is not None:
            data['svg_file'] = SimpleUploadedFile('plan.svg', svg.encode('utf-8'), content_type='image/svg+xml')
        if json_text is not None:
            data['json_file'] = SimpleUploadedFile('plan.json', json_text.encode('utf-8'), content_type='application/json')
        return client.post(self.control_url(), data)
