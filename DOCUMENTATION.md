# Pretix Simple Seating Plan Plugin Documentation

## Overview

**pretix_simpleseatingplan** is a plugin for [Pretix](https://pretix.eu) that adds numbered seating plan management functionality to events. This plugin allows event organizers to create and manage vector seating plans (SVG) and enables customers to select their preferred seats during the booking process.

## Key Features

### 📋 Seating Plan Management

- **SVG Plan Import** : Upload your existing seating plans in SVG format
- **JSON Import** : Compatible with exports from [seats.pretix.eu](https://seats.pretix.eu)
- **Automatic Generation** : Create structured SVG from JSON data
- **Flexible Editing** : Support for custom prefixes for seat identifiers

### 🪑 Seat Reservation System

- **Temporary Booking** : Seats are reserved during the payment process (configurable duration, default 10 minutes)
- **Automatic Release** : Expired reservations are automatically deleted
- **Conflict Management** : Atomic acquisition procedure to prevent double allocations
- **Seat Status** : Real-time tracking of sold and reserved seats

### 🏷️ Seat Categories

- **Category Support** : Organize seats by categories (VIP, standard, economy, etc.)
- **Variation Mapping** : Connect seat categories to Pretix article variations
- **Color Coding** : Each category can be displayed with its own color in the plan

### 📱 Client Interface

- **Interactive Selector** : JavaScript interface for seat selection during checkout
- **Real-time Display** : Instant updates to seat availability
- **Automatic Recording** : Selected seat label is automatically saved to the seat question via JavaScript

## Architecture

### Data Models

#### `SeatingConfig`
Main event configuration:
- `event` : Link to the Pretix event
- `item_id` : Ticketable item linked to the seating plan
- `question_label_id` : ID of the Pretix question where seat labels are stored (auto-created)
- `svg` : SVG code of the seating plan
- `seat_id_prefix` : Prefix for SVG identifiers (default: "seat-")
- `hold_minutes` : Temporary reservation duration in minutes (default: 10)
- `category_variation_map` : Mapping of categories to variation IDs for validation

#### `Seat`
Represents an individual seat:
- `event` : Event the seat belongs to
- `seat_guid` : Unique seat identifier (based on SVG UUID)
- `label` : Human-readable label (e.g., "A-1", "Box 5")
- `category` : Seat category (e.g., VIP, standard)

#### `SeatHold`
Temporary seat reservation:
- `event` : Event
- `seat_guid` : Reserved seat
- `cart_position_id` : Customer's cart position
- `expires` : Reservation expiration timestamp

#### `SeatAssignment`
Final seat assignment after purchase:
- `event` : Event
- `seat_guid` : Assigned seat
- `order_position_id` : Final order position

### Pretix Integration Points

1. **Questions** : The plugin creates a question to store the selected seat label
   - This question is automatically filled by the JavaScript interface when a seat is selected
   - The question response is the primary storage mechanism during checkout
2. **SeatAssignment** : After purchase, a `SeatAssignment` record links the final order position to the seat
3. **Article Variations** : Categories can be mapped to price variations to enforce price zone validation
4. **Permissions** : Uses existing Pretix permission system

## URL Endpoints

The plugin exposes the following endpoints (under `/plugins/pretix_simpleseatingplan/`):

- `GET plan.svg` : Returns the seating SVG plan
- `GET config.js` : Returns dynamic JavaScript configuration
- `GET status` : Current seat status (sold/reserved) in JSON
- `POST hold` : Reserve a seat
- `POST release` : Release a seat reservation
- `GET settings` : Configuration page (admin only)

## Usage Flow

### For Event Organizer

1. Create a "Ticket with Seat" item in Pretix
2. Access plugin settings in the control panel
3. Upload an SVG or JSON plan
4. Configure parameters:
   - SVG identifier prefix
   - Seat reservation duration
   - Category mapping (optional)
5. Automatically import seats from the plan

### For Customer

1. During checkout, a seat selection section appears
2. The interface displays the SVG plan with available seats
3. Clicking a seat temporarily reserves it
4. The selection is recorded when finalizing the order
5. After purchase, the seat is marked as sold

## Configuration

### Configurable Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `seat_id_prefix` | Prefix for SVG seat identifiers | `seat-` |
| `hold_minutes` | Temporary reservation duration | `10` |
| `category_variation_map` | Category → variation mapping (free text) | Empty |

### Category Mapping

Text format with one entry per line:
```
Category I = 3
Category II = 4
VIP Premium = 5
```

Each line maps a category name to a Pretix variation ID.

## Security

- **Holds belong to a cart** : `/hold` and `/release` only accept a `cartpos_id` that is a cart position of the caller's own pretix cart (checked against `session['carts']`). Nobody can hold, steal or release a seat on behalf of another shopper, and each cart position holds at most one seat at a time.
- **Server-side exclusivity** : the final order validation goes through the same `claim_seat()` primitive as `/hold`, so a seat somebody else is in the middle of buying is refused even if its label is typed by hand, and two buyers typing the same free seat cannot both pass validation.
- **SVG sanitization** : the plan is rebuilt through an allow-list (`svg_sanitize.py`) at import **and** on every output path (checkout `config.js`, control-panel preview, audit page, `plan.svg`, ticket image). Scripts, event handlers, `<foreignObject>`, `<style>`, external references and XML entity declarations are dropped.
- **Least privilege** : the seat audit needs "view orders" in addition to "change settings"; applying its fix needs "change orders".
- **Transaction Atomicity** : plan imports run in one transaction (a failed import leaves the previous seats intact); holds rely on the `(event, seat_guid)` unique constraint.
- **CSRF Protection** : POST endpoints use Django's CSRF middleware

## How Seat Selection and Storage Works

### Selection Flow

1. **User Clicks a Seat** : Customer clicks on a seat in the SVG plan during checkout

2. **Server Reservation** : JavaScript calls `POST /hold` to create a temporary `SeatHold` record
   - Reservation expires in 10 minutes by default
   - Atomic database operations prevent double-booking
   - Returns error if seat is already sold or reserved

3. **Form Field Auto-fill** : JavaScript auto-fills the seat input field with the seat label
   - This field is linked to the `question_label_id`
   - The label becomes part of the checkout form data

4. **Cart/Order Validation** : Pretix signals verify the seat selection:
   - A valid `SeatHold` exists for the seat
   - Seat category matches the ticket variation (if configured)
   - Raises `CartError` if validation fails

5. **Final Assignment** : After successful payment:
   - A `SeatAssignment` record is created linking the order position to the seat
   - The question response persists the seat label in the final order

### Storage Locations

Seat information is stored across multiple places for different purposes:

| Storage | Created When | Purpose |
|---------|-------------|----------|
| **Question Response** | During checkout | Primary seat info, visible to customer on ticket |
| **SeatHold** | When `/hold` endpoint is called | Temporary reservation, prevents double-booking |
| **SeatAssignment** | After payment completion | Final relationship, used for admin reports |

## Limitations and Considerations

- Seat storage is tied to Pretix questions (core integration mechanism)
- Does not support multi-seat reservations in a single request (one seat per cart position)
- SVG plans must have proper element structures with `id` or `data-seat-id` attributes
- Seat coordinates use the SVG coordinate system (px)
- Reservation duration is global per event (not customizable per seat)

## Security Considerations

### Attack Prevention

Each item below is covered by regression tests in `tests/backend/`.

#### 1. **Forged or stolen holds** (`test_holds_security.py`)
- **Vulnerability**: `cartpos_id` came straight from the browser and was never checked. An attacker could create a hold carrying *someone else's* cart position id, which `validate_order` then trusted to overwrite that person's "Seat" answer; steal other holds; release other holds; or hold every seat of the plan.
- **Prevention**: `hold()` and `release()` verify that the cart position belongs to a cart of the requester's session (`_owns_cart_position`). A live hold of another cart position is never overwritten (409). A cart position can hold only one seat at a time. Holds whose cart no longer exists ("orphaned") can be taken over.

#### 2. **Double-booking** (`test_order_flow.py`)
- **Vulnerability**: holds were advisory; `validate_order` only refused seats already *sold*, and it runs before pretix takes its order lock.
- **Prevention**: `holds.claim_seat()` creates the hold atomically (unique constraint on `(event, seat_guid)`). `validate_order` claims the seat for the position, so a seat held by another cart is refused and two simultaneous buyers cannot both succeed.
- **Orders created or edited outside the checkout** (REST API, order import, staff or customer edits) never pass through `validate_order`, and pretix offers no hook before they are saved. The plugin therefore relies on `order_placed` / `order_modified`, whose behaviour depends on the context (`test_out_of_checkout.py`):
  - *Shop checkout*: `order_placed` runs inside the transaction that creates the order. If the seat already belongs to another position, it raises an `OrderError`, the whole order is rolled back and the shopper is asked to pick another seat.
  - *REST API and import*: `order_placed` runs **after** the order was committed, so refusing is impossible (raising would leave a committed order behind an error response). The order is kept, the earlier sale is never overwritten, and an entry "Seat conflict" is added to the order's history. The seat audit lists it too.
  - *Edits after the fact*: a seat answer edited to a seat sold to someone else, to a name matching no seat, or emptied is ignored (the ticket keeps its seat) and an entry "Seat change ignored" is added to the order's history.
  - The context is detected with `transaction.get_connection().in_atomic_block`.

#### 3. **Session requirement** (`test_hardening.py`)
- `/status`, `/hold`, `/release` and `/config.js` require a session. This is only a coarse filter (any visitor has a session); the ownership check in item 1 is the real protection.
- `/status` is read-only (expired holds are filtered, not deleted) because every open checkout page polls it every second.

#### 4. **Cross-Site Request Forgery (CSRF)**
- POST endpoints use Django's CSRF middleware; `X-CSRFToken` is sent by `postForm` in seatpicker.js.

#### 5. **XSS and content injection through the SVG** (`test_svg_sanitize.py`, `test_plan_import_security.py`)
- **Vulnerability**: the uploaded SVG was `html.unescape()`d, stored as-is, injected with `innerHTML` into customers' checkout pages and rendered with `|safe` in the control panel. The JSON import interpolated category colours and radii into attributes unescaped.
- **Prevention**: `svg_sanitize.sanitize_svg()` rebuilds the document from an allow-list of elements, attributes and values, rejects DOCTYPE entity declarations, and caps size (5 MB) and element count. It runs at import and, through `clean_plan_svg()`, on every output of the stored plan, because plans stored before this fix may be hostile. JSON colours are validated and radii must be numeric.
- `plan.svg` is served sanitized with `X-Content-Type-Options: nosniff`. Do not add a `Content-Security-Policy` header to it: pretix already sets a strict one and its middleware crashes on directives it does not know (e.g. `sandbox`).

#### 6. **Server-side requests from the ticket renderer**
- The ticket image (cairosvg) only ever receives sanitized SVG, so `file://` / `http://` `<image>` references and XML entities never reach it.

#### 7. **Import robustness** (`test_plan_import_security.py`)
- Malformed, oversized or hostile uploads produce a form error and change nothing (transactional import). Duplicate seat ids in a JSON export no longer crash the import.

#### 8. **Sold seat bypass**
- `hold()` refuses sold seats (409) and unknown seats (400).

#### 9. **Repository hygiene** (`test_repo_hygiene.py`)
- The pretix data directory (`data/`, containing `.secret`, the instance SECRET_KEY) is git-ignored and must never be tracked.

### Endpoint Security Matrix

| Endpoint | Method | Requirement | Validation | CSRF |
|----------|--------|-------------|------------|------|
| `/hold` | POST | session + cart ownership | seat_guid known & unsold, cartpos_id owned | yes |
| `/release` | POST | session + cart ownership | hold must belong to that cart position | yes |
| `/status` | GET | session | read-only | n/a |
| `/config.js` | GET | session | plan sanitized on output | n/a |
| `/plan.svg` | GET | none (public) | plan sanitized on output | n/a |
| control `settings` | GET/POST | change settings | upload sanitized, transactional | yes |
| control `audit` | GET | change settings + view orders | | n/a |
| control `audit` (fix) | POST | + change orders | | yes |

### Recommended Deployment Practices

1. **Use HTTPS Only** : Always serve endpoints over HTTPS to protect session cookies and CSRF tokens
2. **Session Configuration** : Configure Pretix with secure session cookies:
   ```python
   SESSION_COOKIE_SECURE = True
   SESSION_COOKIE_HTTPONLY = True
   CSRF_COOKIE_SECURE = True
   ```
3. **Monitor Integrity** : Set up database triggers or logging to detect suspicious hold patterns
4. **Regular Audits** : Review `SeatHold` and `SeatAssignment` records for inconsistencies

## Troubleshooting

### Seats are not importing

- Verify that the JSON/SVG contains seat identifiers (`seat_guid` or `id`)
- Ensure the configured prefix matches the SVG IDs
- Check the server error console

### Reservations expire too quickly

- Adjust `hold_minutes` in the plugin settings
- Verify that the server has the correct system time

### JavaScript integration is not working

- Run `python -m pretix collectstatic --noinput`
- Verify that `seatpicker.js` and `seatpicker.css` are served correctly
- Check the `STATIC_ROOT` and `STATIC_URL` configuration

## Development

### Code Structure

- `views.py` : Main views and API endpoints
- `holds.py` : `claim_seat()`, the single atomic way to hold a seat
- `svg_sanitize.py` : allow-list SVG sanitizer used on import and on every output
- `models.py` : Data models
- `forms.py` : Configuration forms
- `urls.py` : URL routing
- `signals.py` : Integration with Pretix events
- `static/` : Client-side CSS/JS resources
- `templates/` : HTML templates for admin interface

### Dependencies

- Django (via Pretix)
- Pretix >= 2026.3 (uses the granular `event.*` permission names, which do not exist in earlier releases)
- lxml, cairosvg (ticket image rendering; declared in `setup.cfg`)

### Running the tests

Backend tests run against a real pretix install with pretix's own test settings (in-memory database), from the plugin directory:

```
python -m django test tests.backend --settings=pretix.testutils.settings
```

The pretix environment must have the plugin installed (`pip install -e .`). See `tests/backend/README.md`. The front-end tests in `tests/js` run with `npm test` (needs Node).

## License and Support

This plugin is licensed under Apache License 2.0. See LICENSE for full terms.
