'use strict';
// The "everything went fine" counterpart: two attendees, each typing a
// distinct, genuinely available seat, no clicking on the plan at all.
// Expectation: the order proceeds -- no error anywhere, form actually
// submitted -- so the happy path isn't accidentally caught by validation
// meant for mistakes.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock, submitForm, stopStatusRefresh } = require('./harness');

test('two distinct, available, correctly-typed seats submit cleanly', async (t) => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl, cfgOverrides: { status_url: 'https://example.test/status/' } });
  const { window } = dom;
  t.after(() => stopStatusRefresh(window));
  await waitForBoot(window);

  const input1 = window.document.getElementById('id_101-question_5');
  const input2 = window.document.getElementById('id_102-question_5');
  await typeIntoField(window, input1, 'A-1');
  await typeIntoField(window, input2, 'A-2');

  const form = window.document.querySelector('form');
  const submitted = await submitForm(window, form);

  assert.equal(submitted, true, 'the order must go through when both seats are valid, distinct and available');
  assert.equal(window.document.querySelectorAll('.seat-error').length, 0, 'no field should be flagged');
  assert.equal(window.document.querySelectorAll('.seat-error-msg').length, 0, 'no error message should remain visible');
});
