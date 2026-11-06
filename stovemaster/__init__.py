"""stovemaster - home hub for a Palazzetti stove.

Runs on a Raspberry Pi next to the stove's Connection Box and provides:

* the local dashboard on the home network (http://stove.local, no login),
* a weekly scheduler (per-slot power, fan and setpoint),
* an mDNS responder that advertises stove.local,
* an always-on WebSocket to the website on the VPS for remote access, with
  end-to-end encrypted sessions and push notifications.

Standard library only, plus the "cryptography" package for remote access.
"""

__version__ = "2.0.0"
