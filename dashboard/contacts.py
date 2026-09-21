"""The driver phone book, read-only from the dashboard's point of view.

Deliberately a flat file like driver.py and history.py: it's a short list someone edits by
hand in a text editor when a driver joins or leaves, not something the app writes to.
"""

import json
import re

from pathlib import Path

CONTACTS_PATH = Path(__file__).resolve().parent / "contacts.json"


def load_contacts(path=None):
    """The contact list, or [] if the file is missing/corrupt/not-a-list."""
    path = Path(path) if path is not None else CONTACTS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def wa_link(phone):
    """A wa.me link for a phone number in any of contacts.json's formats.

    wa.me wants digits only (no "+", spaces or dashes) — see
    https://faq.whatsapp.com/425247423261404 for the click-to-chat link format.
    """
    digits = re.sub(r"\D", "", phone)
    return f"https://wa.me/{digits}"
