'use strict';
// Plausible human behavior: transposing the row letter and seat number
// ("1N" instead of "N-1", or here "1A" instead of "A-1") -- a common typo
// pattern. Nothing on the plan matches this text, so no hold is ever
// attempted while typing (guidFromLabel finds nothing, silently). The
// expectation from the task: this must not be allowed to silently pass --
// on submit, it has to be refused with a clear, understandable, highlighted
// error pointing at the offending field, not a generic/blocked submit.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock, submitForm, stopStatusRefresh } = require('./harness');

test('a transposed/garbage seat label ("1A" instead of "A-1") blocks submit with a highlighted error', async (t) => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl, cfgOverrides: { status_url: 'https://example.test/status/' } });
  const { window } = dom;
  t.after(() => stopStatusRefresh(window));
  await waitForBoot(window);

  const input1 = window.document.getElementById('id_101-question_5');
  const input2 = window.document.getElementById('id_102-question_5');
  await typeIntoField(window, input1, '1A'); // transposed -- matches nothing
  await typeIntoField(window, input2, 'A-2'); // a normal, valid, available seat

  assert.equal(server.holdRequests.length, 1, 'the unmatched field never triggers a hold request in the first place');

  // Immediate feedback (before ever touching submit): a "did you mean" hint,
  // not a silent no-op that only surfaces the problem at submit time.
  const legendAfterTyping = window.document.querySelector('.legend').textContent;
  assert.match(
    legendAfterTyping, /vouliez-vous dire.*A-1/i,
    `typing an unmatched-but-close label should immediately hint at the likely intended seat, got: "${legendAfterTyping}"`
  );

  const form = window.document.querySelector('form');
  const submitted = await submitForm(window, form);

  assert.equal(submitted, false, 'the form must not actually be submitted while a field has no valid seat');
  assert.ok(input1.classList.contains('seat-error'), 'the offending field must be visually highlighted');
  assert.ok(!input2.classList.contains('seat-error'), 'the valid field must not be flagged');

  const msg = input1.parentElement.querySelector('.seat-error-msg').textContent;
  assert.match(msg, /n'existe pas/i, `the error must clearly say the seat number doesn't exist, got: "${msg}"`);
  assert.match(
    msg, /vouliez-vous dire.*A-1/i,
    `the submit-time error should also suggest the likely intended seat, got: "${msg}"`
  );
});
