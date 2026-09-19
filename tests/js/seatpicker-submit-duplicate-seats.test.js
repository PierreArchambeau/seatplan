'use strict';
// Plausible human behavior: buying several tickets at once and typing the
// same seat twice by mistake (copy-paste, or genuinely misremembering which
// seat was already used for the first attendee) -- possibly with different
// formatting each time ("A-1" then "a 1"), which must still be recognized
// as the same seat. Expectation: refused at submit with both offending
// fields clearly highlighted, not a silent acceptance of a physically
// impossible order (two attendees on one chair).
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock, submitForm, stopStatusRefresh } = require('./harness');

test('typing the same seat twice (different formatting) blocks submit with both fields highlighted', async (t) => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl, cfgOverrides: { status_url: 'https://example.test/status/' } });
  const { window } = dom;
  t.after(() => stopStatusRefresh(window));
  await waitForBoot(window);

  const input1 = window.document.getElementById('id_101-question_5');
  const input2 = window.document.getElementById('id_102-question_5');
  await typeIntoField(window, input1, 'A-1');
  // Local anti-doublon (g.seatToInput) already suppresses the second hold
  // attempt for the exact same normalized seat -- but the field still shows
  // this raw duplicate text, so submit-time validation is what must catch it.
  await typeIntoField(window, input2, 'a 1');

  assert.equal(
    server.holdRequests.length, 1,
    'the second field must not even attempt to hold a seat its sibling field already tracks locally'
  );

  const form = window.document.querySelector('form');
  const submitted = await submitForm(window, form);

  assert.equal(submitted, false, 'the form must not be submitted with two attendees on the same seat');
  assert.ok(input1.classList.contains('seat-error'), 'the first duplicate field must be highlighted');
  assert.ok(input2.classList.contains('seat-error'), 'the second duplicate field must be highlighted');

  const msg = input2.parentElement.querySelector('.seat-error-msg').textContent;
  assert.match(msg, /déjà attribué/i, `the error must clearly explain the seat is already used, got: "${msg}"`);
});
