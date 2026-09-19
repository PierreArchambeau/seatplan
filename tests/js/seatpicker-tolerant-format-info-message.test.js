'use strict';
// Plausible human behavior: typing a seat number that is understood but not
// in the exact canonical format shown on the plan (e.g. "a1" for "A-1", no
// dash, wrong case). The match still succeeds (tolerant matching), but the
// user gets no feedback about which seat they actually got -- worth an
// informational message confirming what was understood, without treating
// it as an error since the reservation did succeed.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock } = require('./harness');

test('typing a loosely-formatted but understood seat label shows an info message with the canonical label', async () => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl });
  const { window } = dom;
  await waitForBoot(window);

  const input = window.document.getElementById('id_101-question_5');
  await typeIntoField(window, input, 'a1'); // canonical label on the plan is "A-1"

  assert.equal(server.holdRequests.at(-1).seat_guid, 'a1', 'the tolerant match still resolves to the right seat');

  const legend = window.document.querySelector('.legend');
  assert.match(
    legend.textContent, /reconnu.*A-1/i,
    `an info message should confirm which seat the loose input was understood as, got: "${legend.textContent}"`
  );

  const circle = window.document.querySelector('#seat-a1 circle');
  assert.equal(circle.getAttribute('fill'), '#2563eb', 'the seat is still correctly held and shown as selected');
});

test('typing the exact canonical seat label does not show the "understood as" info message', async () => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl });
  const { window } = dom;
  await waitForBoot(window);

  const input = window.document.getElementById('id_101-question_5');
  await typeIntoField(window, input, 'A-1'); // already exactly the canonical label

  const legend = window.document.querySelector('.legend');
  assert.doesNotMatch(
    legend.textContent, /reconnu/i,
    `no reformatting happened, so no "understood as" message is needed, got: "${legend.textContent}"`
  );
});
