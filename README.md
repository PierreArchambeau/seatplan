# pretix_simpleseatingplan

A Pretix plugin for managing numbered and interactive seating plans.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Pretix Compatible: 2026.1.x](https://img.shields.io/badge/Pretix-2026.1.x-green.svg)](https://pretix.eu)

## 📌 About

**pretix_simpleseatingplan** allows event organizers using Pretix to add comprehensive seating plan management functionality to their events. Customers can select their preferred seats during the ticket purchase process, and organizers benefit from advanced management tools.

## ✨ Features

- 🪑 **Interactive Seat Selection** - User-friendly and responsive client interface
- 📥 **Flexible Import** - Direct SVG support and JSON (seats.pretix.eu)
- ⏱️ **Reservation System** - Temporary booking with automatic expiration
- 🏷️ **Seat Categories** - Organization by price zones with color coding
- 🔄 **Real-time Synchronization** - Instant availability updates
- 🔐 **Secure** - Protection against race conditions and XSS exploits

For detailed documentation, see [DOCUMENTATION.md](DOCUMENTATION.md).

## ⚡ Quick Start

### Installation

```bash
# 1. Clone or copy the plugin directory
cp -r pretix_simpleseatingplan /path/to/pretix/plugins/

# 2. Install dependencies
cd /path/to/pretix
pip install -e ./plugins/pretix_simpleseatingplan/

# 3. Apply migrations
python -m pretix migrate

# 4. Collect static files
python -m pretix collectstatic --noinput

# 5. Restart the server
# For gunicorn:
systemctl restart pretix
```

### Event Configuration

1. In the Pretix control panel, navigate to your event
2. Go to **Settings** → **Plugins** and enable `pretix_simpleseatingplan`
3. Go to **Plugin Settings** → **Simple Seating Plan**
4. Configure your seating plan:
   - Upload an SVG or JSON file
   - Select the associated ticket item
   - Adjust reservation duration if needed
5. Click **Save** to import the seats

## 📋 Supported Import Formats

### SVG Format

SVG plans must contain elements with the following attributes:
- `id` : Must start with the configured prefix (e.g., `seat-A1`)
- Optional: `data-seat-label`, `data-seat-category`

### JSON Format

Import compatible with exports from [seats.pretix.eu](https://seats.pretix.eu):
```json
{
  "zones": [{
    "rows": [{
      "seats": [{
        "uuid": "unique-id",
        "seat_guid": "A1",
        "seat_number": "1",
        "category": "Standard",
        "position": {"x": 0, "y": 0}
      }]
    }]
  }]
}
```

## 🔧 Configuration

### Main Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Seat Prefix** | Prefix for SVG identifiers | `seat-` |
| **Reservation Duration** | Time before expiration (minutes) | `10` |
| **Category Mapping** | Connect category → variation | — |

### Category Mapping (Optional)

Connect seat categories to article variations:
```
VIP = 1
Standard = 2
Economy = 3
```

## 📊 Architecture

The plugin uses four main models:

- **SeatingConfig** : Event configuration
- **Seat** : Individual seat (immutable after import)
- **SeatHold** : Temporary reservation during checkout
- **SeatAssignment** : Final assignment after purchase

For more details, see [DOCUMENTATION.md](DOCUMENTATION.md#architecture).

## 🚀 Production Deployment Checklist

- ✓ Copy the plugin directory to your environment
- ✓ Run `pip install -e .` (or `pip install .`)
- ✓ Enable the plugin in Pretix event settings
- ✓ Run `python -m pretix migrate`
- ✓ Run `python -m pretix collectstatic --noinput` (critical for CSS/JS)
- ✓ Restart the server (gunicorn/uwsgi)
- ✓ Test seat selection in test checkout
- ✓ Check server logs for errors

## 🐛 Troubleshooting

- **Seats are not appearing** : Verify that `collectstatic` has been run and STATIC_ROOT/STATIC_URL are configured
- **Reservations expire too quickly** : Increase `hold_minutes` in settings
- **"No SVG in config" error** : Verify that a valid SVG/JSON plan has been uploaded

For more help, see [DOCUMENTATION.md](DOCUMENTATION.md#troubleshooting).

## 📄 License

This plugin is provided under the Apache License 2.0. See [LICENSE](LICENSE).

## 🙌 Contributing

Contributions are welcome and encouraged.

- Open an issue to report bugs or discuss ideas
- Submit a pull request for fixes, tests, or improvements
- Help improve documentation and examples

Even small contributions are valuable and appreciated.

### How to Contribute

1. Fork the repository and create a feature branch.
2. Set up a local Pretix environment and install the plugin in editable mode:

```bash
pip install -e .
python -m pretix migrate
python -m pretix collectstatic --noinput
```

3. Make focused changes with clear commit messages.
4. Validate your changes locally:

```bash
python -m pretix check
```

5. Update documentation when behavior or configuration changes.
6. Open a pull request with:
- A short problem statement
- A summary of the solution
- Manual test steps (and screenshots if UI changed)

### Good First Contributions

- Improve error handling and validation messages
- Add tests around hold/release and cart validation flows
- Improve SVG import compatibility and docs examples

## 🤝 Support

This plugin is developed on a volunteer basis and in contributors' available time.
Response times are not guaranteed.
Community contributions are welcome and help improve support and delivery speed for everyone.

For bugs or feature requests, please open an issue.
