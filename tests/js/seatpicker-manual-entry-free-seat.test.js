'use strict';
// Reproduces the exact scenario reported: no prior click on the plan, the
// user moves focus straight into an empty "Seat" question field and types a
// genuinely free seat's label. Expected: the seat is held (server returns
// 200 ok) and painted blue, nothing else. Reported bug: a "this seat is
// already selected/held" message appears instead.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField } = require('./harness');

test('typing a genuinely free seat, first interaction, no prior click: hold succeeds cleanly', async () => {
  const holdRequests = [];
  const fetchImpl = async (url, opts) => {
    const u = String(url);
    if (u.includes('/hold/')) {
      const body = new URLSearchParams(opts.body);
      holdRequests.push(Object.fromEntries(body.entries()));
      // The server genuinely has nothing held/sold for this seat: always ok.
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

  const input = window.document.getElementById('id_101-question_5');
  assert.equal(input.value, '', 'sanity check: field starts empty, nothing clicked yet');

  await typeIntoField(window, input, 'A-1');

  assert.equal(holdRequests.length, 1, `exactly one /hold/ request should have been sent (got ${holdRequests.length})`);

  const legend = window.document.querySelector('.legend');
  assert.doesNotMatch(
    legend.textContent, /déjà|réservé|vendu/i,
    `no "already selected/held/sold" message should appear for a genuinely free seat, got: "${legend.textContent}"`
  );

  const circle = window.document.querySelector('#seat-a1 circle');
  assert.equal(circle.getAttribute('fill'), '#2563eb', 'the seat should be painted as selected (blue)');
});
