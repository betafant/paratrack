"""How an aircraft is named on screen."""

from __future__ import annotations


def label_for(pilot_name: str | None, competition_id: str | None, registration: str | None, callsign: str) -> str:
    """Pilot name (FANET), else competition number and registration, else either, else the callsign.

    Registration and competition number are only ever stored for owners who agreed to be identified.
    """
    if pilot_name:
        return pilot_name
    if competition_id and registration:
        return f"{competition_id} {registration}"
    return registration or competition_id or callsign
