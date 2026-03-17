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

- **Session Verification** : All cart/checkout endpoints verify a valid session or authentication
- **Owner Verification** : The `/release` endpoint only allows users to release their own seat holds (by validating `cart_position_id`)
- **Input Validation** : All user inputs are validated (seat_guid, cartpos_id converted to integers)
- **XSS Protection** : SVGs are served with appropriate headers (`X-Content-Type-Options: nosniff`)
- **Transaction Atomicity** : Reservations use Django transactions to prevent race conditions and double-booking
- **CSRF Protection** : Form endpoints use Django's CSRF middleware

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

The plugin implements multiple security layers to prevent common attacks:

#### 1. **Cross-User Hold Theft** (FIXED)
- **Vulnerability**: A user could release another user's seat reservation
- **Prevention**: The `/release` endpoint requires the correct `cart_position_id` - users can only release holds they created
- **Implementation**: Deletion filter includes `cart_position_id` in the WHERE clause

#### 2. **Double-Booking Prevention**
- **Vulnerability**: Two users could book the same seat in a race condition
- **Prevention**:
  - `hold()` endpoint uses Django's `transaction.atomic()` for atomicity
  - Unique constraint on `(event, seat_guid)` in `SeatHold` model
  - Returns `IntegrityError` (409 Conflict) if seat is already held
- **Flow**: Check → Create in atomic block prevents TOCTOU race conditions

#### 3. **Session Hijacking / Unauthorized Access**
- **Vulnerability**: Users without an active session could manipulate holds
- **Prevention**:
  - All cart/checkout endpoints verify valid session: `request.session.session_key` OR `request.user.is_authenticated`
  - Unauthenticated requests get 403 Forbidden
- **Scope**: `/hold`, `/release`, `/status`, `/config.js` all protected

#### 4. **Cross-Site Request Forgery (CSRF)**
- **Vulnerability**: Malicious sites could trigger seat holds on behalf of users
- **Prevention**:
  - `POST` endpoints use Django's CSRF middleware
  - `X-CSRFToken` header required (enforced by `postForm` in seatpicker.js)
  - GET requests (status, config, SVG) don't modify state, so CSRF-safe

#### 5. **XSS via SVG Injection**
- **Vulnerability**: Malicious SVG containing `<script>` tags could execute code
- **Prevention**:
  - `plan_svg()` endpoint sets headers:
    - `X-Content-Type-Options: nosniff` - prevents browser content-type sniffing
    - `Content-Disposition: inline` - controlled display mode
  - SVG content is HTML-escaped when imported
- **Note**: SVG filter element attacks are browser-specific; sanitization library not used but not necessary here as SVG is uploaded by admin

#### 6. **Invalid Cart Position ID**
- **Vulnerability**: Users could submit fake cart position IDs
- **Prevention**:
  - `hold()` and `release()` validate numeric cartpos_id format
  - Invalid IDs are rejected with 400 Bad Request
  - `release()` checks deletion count; returns 404 if no matching hold found

#### 7. **Sold Seat Bypass**
- **Vulnerability**: Users could try to hold already-sold seats
- **Prevention**:
  - `hold()` checks `SeatAssignment.objects.filter(...).exists()` before allowing hold
  - Returns 409 Conflict if seat is sold
  - Prevents hold creation on purchased seats

### Endpoint Security Matrix

| Endpoint | Method | Session Check | Validation | Atomic | CSRF |
|----------|--------|--------------|------------|--------|------|
| `/hold` | POST | ✓ | ✓ seat_guid, cartpos_id | ✓ | ✓ |
| `/release` | POST | ✓ | ✓ + cart_position_id match | — | ✓ |
| `/status` | GET | ✓ | ✓ event validation | — | — |
| `/config.js` | GET | ✓ | ✓ question check | — | — |
| `/plan.svg` | GET | — | ✓ event validation | — | — |

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
- `models.py` : Data models
- `forms.py` : Configuration forms
- `urls.py` : URL routing
- `signals.py` : Integration with Pretix events
- `static/` : Client-side CSS/JS resources
- `templates/` : HTML templates for admin interface

### Dependencies

- Django (via Pretix)
- Pretix >= 2026.1.x

## License and Support

This plugin is licensed under Apache License 2.0. See LICENSE for full terms.
