'use strict';
// Plausible human behavior: misunderstanding the UI and typing several seat
// numbers into a single "Seat" field (e.g. copy-pasting "A-1, A-2" meant to
// cover a whole group), and doing this in every seat field of the order
// rather than one seat per attendee. None of that text matches any single
// seat's label, so no hold is ever attempted while typing -- the important
// guarantee is that submit-time validation still catches it and explains
// the problem, instead of silently letting an order through with no real
// seats attached to it.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock, submitForm, stopStatusRefresh } = require('./harness');

test('typing several seat numbers into every seat field blocks submit with each field highlighted', async (t) => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl, cfgOverrides: { status_url: 'https://example.test/status/' } });
  const { window } = dom;
  t.after(() => stopStatusRefresh(window));
  await waitForBoot(window);

  const input1 = window.document.getElementById('id_101-question_5');
  const input2 = window.document.getElementById('id_102-question_5');
  await typeIntoField(window, input1, 'A-1, A-2');
  await typeIntoField(window, input2, 'A-1 et A-2');

  assert.equal(server.holdRequests.length, 0, 'text that matches no single seat must never trigger a hold request');

  const form = window.document.querySelector('form');
  const submitted = await submitForm(window, form);

  assert.equal(submitted, false, 'an order with no field containing one real seat each must not go through');
  assert.ok(input1.classList.contains('seat-error'), 'the first field must be highlighted');
  assert.ok(input2.classList.contains('seat-error'), 'the second field must be highlighted');

  for (const input of [input1, input2]) {
    const msg = input.parentElement.querySelector('.seat-error-msg').textContent;
    assert.match(msg, /n'existe pas/i, `each field must clearly explain its content isn't a valid seat, got: "${msg}"`);
  }
});
