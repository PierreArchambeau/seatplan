'use strict';
// Reproduces: "no prior click on the plan, focus straight into an empty
// Seat field, type a genuinely free seat, observe the message" -> reported
// as "Ce siège est réservé par un autre utilisateur." even though nothing
// else ever held it.
//
// Root cause: boot() is invoked many times per page load in real usage
// (setTimeout retries at 100/500/1000/2000/3000ms, a MutationObserver, and
// the 'pretix:ui:changed' event -- see the bottom of seatpicker.js) so that
// it can pick up Seat fields that appear after the initial render. Before
// the fix, every one of those calls re-attached a *new* focus/blur listener
// closure to each already-known input, because removeEventListener could
// never match a listener added by a previous call's different closure.
// A single blur then fired all of the stacked listeners, each independently
// POSTing a hold request for the same seat; only the first can win the
// server's uniqueness constraint, so the rest come back 409 "held" and that
// message ends up on screen -- for a seat nobody else ever touched.
//
// This test drives boot() through several extra invocations the same way
// real pages do (dispatching 'pretix:ui:changed', one of its real retrigger
// hooks) before ever touching the field, then types into a still-empty,
// genuinely free seat field exactly once.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField } = require('./harness');

test('typing a free seat after boot() has re-run several times sends exactly one hold request', async () => {
  const holdSeatGuids = [];
  const heldGuids = new Set(); // simulates the server's real SeatHold uniqueness constraint
  const fetchImpl = async (url, opts) => {
    const u = String(url);
    if (u.includes('/hold/')) {
      const body = new URLSearchParams(opts.body);
      const guid = body.get('seat_guid');
      holdSeatGuids.push(guid);
      if (heldGuids.has(guid)) {
        return { ok: false, status: 409, json: async () => ({ ok: false, error: 'held' }) };
      }
      heldGuids.add(guid);
      return { ok: true, status: 200, json: async () => ({ ok: true, expires: new Date().toISOString() }) };
    }
    if (u.includes('/release/')) {
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    throw new Error('Unexpected fetch: ' + u);
  };

  const dom = makeDom({ fetchImpl });
  const { window } = dom;
  await waitForBoot(window);

  // Simulate the real retry schedule re-invoking boot() several more times
  // before the user ever interacts with the field (as setTimeout/MutationObserver
  // do in the real page).
  for (let i = 0; i < 5; i++) {
    window.document.dispatchEvent(new window.Event('pretix:ui:changed'));
  }
  await new Promise((r) => setTimeout(r, 20));

  const input = window.document.getElementById('id_101-question_5');
  assert.equal(input.value, '', 'sanity check: nothing clicked/typed yet');

  await typeIntoField(window, input, 'A-1'); // A-1 is genuinely free; nothing else ever requested it

  assert.equal(
    holdSeatGuids.length, 1,
    `a single blur on a single field must send exactly one /hold/ request, got ${holdSeatGuids.length} ` +
    `(duplicate event listeners from repeated boot() calls would send one per listener)`
  );

  const legend = window.document.querySelector('.legend');
  assert.doesNotMatch(
    legend.textContent, /réservé|déjà|vendu/i,
    `a genuinely free seat, touched only once, must not show an unavailability message, got: "${legend.textContent}"`
  );
});
