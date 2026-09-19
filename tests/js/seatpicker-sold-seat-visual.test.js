'use strict';
// Regression test for: typing an already-SOLD seat's label manually into a
// "Seat" question field correctly shows the "Ce siège est déjà vendu."
// message (the server's hold endpoint rejects it), but the seat still gets
// painted blue ("selected") on the plan -- as if the hold had succeeded --
// even though clicking that same seat on the plan is blocked entirely.
//
// Root cause: refreshSelectedVisuals() paints a seat as "selected" purely by
// matching the *current text* of every Seat input field against the plan's
// seat labels. It never checks whether a hold for that text actually
// succeeded (that bookkeeping lives in g.inputToGuid / g.seatToInput, which
// onBlurSeatInput deliberately does NOT update when tryHoldIfConfigured
// returns false) -- so a rejected seat, whose invalid text is left sitting
// in the field, still gets repainted as if selected on every visual refresh.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField } = require('./harness');

function fetchMockRejectingSoldSeat(soldSeatGuid) {
  return async (url, opts) => {
    const u = String(url);
    if (u.includes('/hold/')) {
      const body = new URLSearchParams(opts.body);
      if (body.get('seat_guid') === soldSeatGuid) {
        return {
          ok: false, status: 409,
          json: async () => ({ ok: false, error: 'sold' }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true, expires: new Date().toISOString() }) };
    }
    if (u.includes('/release/')) {
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    throw new Error('Unexpected fetch: ' + u);
  };
}

test('manually typing an already-sold seat shows the sold message and does NOT paint it as selected', async () => {
  const dom = makeDom({ fetchImpl: fetchMockRejectingSoldSeat('a1') });
  const { window } = dom;
  await waitForBoot(window);

  const input = window.document.getElementById('id_101-question_5');
  await typeIntoField(window, input, 'A-1'); // A-1 == seat-a1, the "sold" seat in the mock

  const legend = window.document.querySelector('.legend');
  assert.match(legend.textContent, /vendu/i, 'the "already sold" message should be shown');

  const circle = window.document.querySelector('#seat-a1 circle');
  assert.notEqual(
    circle.getAttribute('fill'), '#2563eb',
    'a seat the server rejected as sold must not be painted as "selected" (blue) on the plan'
  );
});

test('manually typing a genuinely free seat is accepted and IS painted as selected', async () => {
  const dom = makeDom({ fetchImpl: fetchMockRejectingSoldSeat('a1') }); // only a1 is "sold" in this mock
  const { window } = dom;
  await waitForBoot(window);

  const input = window.document.getElementById('id_102-question_5');
  await typeIntoField(window, input, 'A-2'); // a2 is free in the mock

  const legend = window.document.querySelector('.legend');
  assert.doesNotMatch(legend.textContent, /vendu|réservé/i, 'a free seat should not show an unavailability message');

  const circle = window.document.querySelector('#seat-a2 circle');
  assert.equal(
    circle.getAttribute('fill'), '#2563eb',
    'a seat that was actually successfully held should be painted as selected (blue)'
  );
});
