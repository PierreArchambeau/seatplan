'use strict';
// Plausible human behavior: picking a seat, then changing your mind and
// retyping a different one in the same field -- no click on the plan
// involved at all. Expectation: the first seat is released server-side
// (so it doesn't stay locked away from other buyers) and the new one is
// held instead; the field ends up correctly tracking only the new seat.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock } = require('./harness');

test('retyping a different seat in the same field releases the old hold and takes the new one', async () => {
  const server = makeServerMock();
  const dom = makeDom({ fetchImpl: server.fetchImpl });
  const { window } = dom;
  await waitForBoot(window);

  const input = window.document.getElementById('id_101-question_5');
  await typeIntoField(window, input, 'A-1');
  assert.equal(server.holdRequests.at(-1).seat_guid, 'a1');
  assert.ok(server.held.has('a1'), 'A-1 is held after the first entry');

  await typeIntoField(window, input, 'A-2'); // changed their mind, no click involved

  assert.equal(server.releaseRequests.length, 1, 'the old seat must be explicitly released');
  assert.equal(server.releaseRequests[0].seat_guid, 'a1', 'the release must target the seat that was actually left behind');
  assert.equal(server.holdRequests.at(-1).seat_guid, 'a2', 'the new seat must be held');
  assert.ok(!server.held.has('a1'), 'A-1 must no longer be held by anyone once released');
  assert.ok(server.held.has('a2'), 'A-2 is now held instead');

  const legend = window.document.querySelector('.legend');
  assert.doesNotMatch(legend.textContent, /vendu|réservé/i, 'switching to a free seat must not show an unavailability message');

  const circleA2 = window.document.querySelector('#seat-a2 circle');
  assert.equal(circleA2.getAttribute('fill'), '#2563eb', 'the newly chosen seat is shown as selected');
});
