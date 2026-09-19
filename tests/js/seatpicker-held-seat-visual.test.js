'use strict';
// Same family of bug as the "sold" case, but for a seat someone else is
// merely holding right now (not sold yet) -- a real, legitimate collision
// between two shoppers, as opposed to the earlier duplicate-listener bug.
// Plausible human behavior: two people browsing the same event at the same
// time, one clicks/types a seat a few seconds before the other tries the
// same one. Expectation: a distinct "temporarily reserved" message, and the
// seat must not be painted as if it had been successfully selected.
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeDom, waitForBoot, typeIntoField, makeServerMock } = require('./harness');

test('typing a seat someone else is currently holding shows the held message, not painted selected', async () => {
  const server = makeServerMock({ held: new Set(['a1']) }); // another shopper got there first
  const dom = makeDom({ fetchImpl: server.fetchImpl });
  const { window } = dom;
  await waitForBoot(window);

  const input = window.document.getElementById('id_101-question_5');
  await typeIntoField(window, input, 'A-1');

  const legend = window.document.querySelector('.legend');
  assert.match(legend.textContent, /réservé par un autre utilisateur/i);

  const circle = window.document.querySelector('#seat-a1 circle');
  assert.notEqual(circle.getAttribute('fill'), '#2563eb', 'a seat someone else is holding must not be shown as selected');
});
