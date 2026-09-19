'use strict';
// Loads the real seatpicker.js into a jsdom window and boots it, so tests
// exercise the actual production code path (DOM events in, DOM state out)
// instead of a reimplementation of its logic.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const SEATPICKER_PATH = path.join(
  __dirname, '..', '..', 'pretix_simpleseatingplan', 'static',
  'pretix_simpleseatingplan', 'frontend', 'seatpicker.js'
);
const SEATPICKER_SRC = fs.readFileSync(SEATPICKER_PATH, 'utf8');

const SVG_FIXTURE = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">
  <g id="seat-a1" data-seat-id="a1" data-seat-label="A-1" data-seat-category="">
    <circle class="seat-dot" cx="50" cy="50" r="12" fill="#22c55e" stroke="#0f172a" stroke-width="1"/>
    <text x="50" y="53" text-anchor="middle" font-size="10">1</text>
  </g>
  <g id="seat-a2" data-seat-id="a2" data-seat-label="A-2" data-seat-category="">
    <circle class="seat-dot" cx="90" cy="50" r="12" fill="#22c55e" stroke="#0f172a" stroke-width="1"/>
    <text x="90" y="53" text-anchor="middle" font-size="10">2</text>
  </g>
</svg>`;

/**
 * Builds a jsdom environment shaped like a real pretix checkout "questions"
 * page: a form with #questions_group and two "Seat" question inputs named
 * the way pretix names them ("{cartpos_id}-question_{question_id}"), plus a
 * csrf meta tag. `fetchImpl` stands in for the network (hold/release/status
 * endpoints); tests control its responses to simulate server state.
 */
function makeDom({ fetchImpl, cfgOverrides = {} } = {}) {
  const html = `<!doctype html><html><head>
    <meta name="csrf-token" content="test-csrf">
  </head><body>
    <form class="checkout-form">
      <div id="questions_group">
        <div class="form-group cart-position" data-cartpos="101">
          <label for="id_101-question_5">Siège</label>
          <input type="text" id="id_101-question_5" name="101-question_5" value="">
        </div>
        <div class="form-group cart-position" data-cartpos="102">
          <label for="id_102-question_5">Siège</label>
          <input type="text" id="id_102-question_5" name="102-question_5" value="">
        </div>
      </div>
    </form>
  </body></html>`;

  const dom = new JSDOM(html, { url: 'https://example.test/checkout/questions/', runScripts: 'outside-only' });
  const { window } = dom;

  window.fetch = fetchImpl;
  window.SimpleSeatingPlanCfg = Object.assign({
    svg: SVG_FIXTURE,
    prefix: 'seat-',
    question_label_id: 5,
    hold_url: 'https://example.test/hold/',
    release_url: 'https://example.test/release/',
    // status_url intentionally omitted: keeps the periodic auto-refresh
    // (scheduleRefresh) out of these tests so they isolate the behavior
    // triggered directly by typing into the field.
  }, cfgOverrides);

  window.eval(SEATPICKER_SRC);
  // seatpicker.js only starts itself via a 'DOMContentLoaded' listener; jsdom
  // may have already fired that event (or fire it asynchronously later, on
  // its own schedule) before/after this listener was registered above, so
  // dispatch it explicitly here for a deterministic boot instead of racing it.
  window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));
  return dom;
}

/** Waits for the async boot() (triggered by DOMContentLoaded) to finish
 * setting up the plan, identified by the legend/container being present. */
async function waitForBoot(window) {
  for (let i = 0; i < 50; i++) {
    if (window.document.querySelector('[data-seatmap] svg')) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error('seatpicker did not boot in time');
}

/** Simulates a user typing `value` into `input`: focus, set value, blur --
 * matching what onFocusSeatInput/onBlurSeatInput actually listen for. */
async function typeIntoField(window, input, value) {
  input.dispatchEvent(new window.Event('focus'));
  input.value = value;
  input.dispatchEvent(new window.Event('blur'));
  // onBlurSeatInput is async (awaits the hold request); flush microtasks/timers.
  await new Promise((r) => setTimeout(r, 20));
}

module.exports = { makeDom, waitForBoot, typeIntoField, SVG_FIXTURE };
