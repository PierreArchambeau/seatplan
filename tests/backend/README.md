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

Run a single module or test:

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
| `test_hardening.py` | read-only `status` polling, `hold_minutes` bounds |
| `test_repo_hygiene.py` | secrets / the pretix data directory are not tracked by git |

`base.py` builds an event with the plugin enabled and a 3-seat plan, and provides helpers to act
as independent shoppers (`new_shopper()`, each with its own cookie jar and pretix cart) and as a
control-panel user with chosen permissions (`admin_client(...)`).
