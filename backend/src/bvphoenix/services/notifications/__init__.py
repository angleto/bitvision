"""Notifications package — outbound reminders to patient contacts.

Public surface:

* :class:`NotificationPayload` — frozen delivery descriptor
* :class:`NotificationResult` — outcome of one send attempt
* :class:`Notifier` — channel-agnostic ABC
* :func:`build_payload` — service helper that turns a
  ``NotificationDispatch`` row + a rendered template into a payload
* :func:`dispatch_notification` — top-level entry called by the
  arq worker (workers/tasks/dispatch_notification.py)
* :func:`materialise_dispatches` — post-commit listener that
  inserts one row per (target, contact, offset, channel) when a
  reminder-bearing event / task lands or is edited

The package owns the dispatcher state machine; channel-specific
implementations live in sibling modules (``email_notifier.py``,
``webhook_notifier.py``, ``ics_attachment.py``).
"""

from bvphoenix.services.notifications.base import (
    NOTIFIER_REGISTRY,
    NotificationChannel,
    NotificationKind,
    NotificationPayload,
    NotificationResult,
    NotificationTargetKind,
    Notifier,
)

__all__ = [
    "NOTIFIER_REGISTRY",
    "NotificationChannel",
    "NotificationKind",
    "NotificationPayload",
    "NotificationResult",
    "NotificationTargetKind",
    "Notifier",
]
