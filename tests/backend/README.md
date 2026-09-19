# Backend regression tests

These tests run against a **real pretix installation** (no mocks of pretix), using
pretix's own test settings: in-memory database, no migrations, a temporary data
directory. They never touch your development database.

## Running them

From the plugin directory (`seatplan/`), with the Python environment where pretix and
this plugin (`pip install -e .`) are installed:

```
python -m django test tests.backend --settings=pretix.testutils.settings
```

With pytest (needs `pip install pytest pytest-django`; configured in `pytest.ini`, and used by
VS Code's Testing panel):

```
python -m pytest
```

Run a single module or test with the Django runner:

```
python -m django test tests.backend.test_holds_security --settings=pretix.testutils.settings
python -m django test tests.backend.test_holds_security.HoldOwnershipTests.test_owner_can_hold_and_release --settings=pretix.testutils.settings
```

On Windows a `PermissionError ... csp.log` traceback may be printed at the very end: it is
pretix's temporary directory being cleaned up while a log file is still open, and is unrelated
to the test results (look at the `Ran N tests ... OK` line).

## What is covered

| File | Covers |
|------|--------|
| `test_holds_security.py` | hold/release ownership: no forged, stolen or hoarded holds |
| `test_order_flow.py` | validate_cart / validate_order / order_placed / order_modified / audit, and server-side seat exclusivity |
| `test_svg_sanitize.py` | the SVG allow-list sanitizer (hostile input neutralised, real plan markup preserved) |
| `test_plan_import_security.py` | plan upload paths and every place the stored plan is rendered |
| `test_audit_permissions.py` | who may open / use the seat audit |
| `test_audit_command.py` | the audit logic (never overwrites a sale, ignores cancelled orders, idempotent fix) and the `simpleseating_audit` command, run with no django-scopes scope active like the real CLI |
| `test_out_of_checkout.py` | seats sold outside the checkout: real `_perform_order` (refused and rolled back), real REST API (kept, conflict recorded), edits after the fact, order history texts |
| `test_ticket_image.py` | the ticket plan image: real PNG pixels, shape highlighting, layout image variable |
| `test_ticket_check_command.py` | the `simpleseating_ticket_check` diagnostic: healthy setup, each broken link (layout, cairo, plan, seat, cache) is named, the plan is told apart from other images on the ticket, and the check can run inside a Celery worker |
| `test_ticket_cache.py` | cached ticket PDFs are invalidated when the plugin changes a seat assignment or the plan |
| `test_ticket_pdf.py` | real ticket PDFs from pretix's PDF output: the plan is on the page, the right seat is highlighted (pixel checks), one page per ticket, grey placeholder when no seat resolves |
| `test_banktransfer_qr.py` | bank transfer payment on the real order page: payment details and the QR payload (valid EPC / BezahlCode), and no QR when it would be wrong. Runs for a German account and for the real Belgian account of the Lions Club de Huy (BNP Paribas Fortis, EPC QR only) |
| `test_banktransfer_qr_browser.py` | the same QR as a real headless Edge/Chrome displays it: screenshot taken, QR decoded, compared with the expected payload. Skipped without a browser or `pip install zxing-cpp`; `SEATPLAN_SKIP_BROWSER_TESTS=1` skips it, `SEATPLAN_TEST_BROWSER` points to a browser, `SEATPLAN_KEEP_SCREENSHOT=<file>` saves the screenshot |
| `test_signals_misc.py` | navigation, presale `<head>`, category mapping, `order_placed`/`order_modified` branches, expiry |
| `test_hardening.py` | read-only `status` polling, `hold_minutes` bounds |
| `test_repo_hygiene.py` | secrets / the pretix data directory are not tracked by git |

`SeatingTestCase` runs each test in a rolled-back transaction; `SeatingTransactionTestCase` (slower) uses real commits, needed when behaviour depends on whether a signal fires inside or after the order's transaction.

`base.py` builds an event with the plugin enabled and a 3-seat plan, and provides helpers to act
as independent shoppers (`new_shopper()`, each with its own cookie jar and pretix cart) and as a
control-panel user with chosen permissions (`admin_client(...)`).
