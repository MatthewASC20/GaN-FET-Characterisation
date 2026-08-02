"""Guards in front of adding and removing a device.

Removing a device deletes every run and sample recorded against it. That is
the most destructive thing this UI can do to bench records, and the records
cannot be recreated without repeating the measurements — so the order of the
guards, and the fact that the busy check comes first, are worth holding in
place. Kept tkinter-free so they can be checked without a display.
"""

from __future__ import annotations

import pytest

from gan_fet.ui.device_actions import (
    device_after_removal,
    removal_confirmation,
    removal_refusal,
    removal_report,
    validated_new_device_name,
)
from gan_fet.ui.run_request import InputRejected

KNOWN = ("EPC2001C", "GS66508B")


# -- removal guards ----------------------------------------------------------


def test_a_known_device_can_be_removed_when_the_rig_is_idle():
    assert (
        removal_refusal(busy=False, device_name="EPC2001C", known_devices=KNOWN)
        is None
    )


def test_a_busy_rig_refuses_before_anything_else_is_considered():
    """A delete landing mid-run would pull rows out from under an executing
    experiment. Busy is checked first, so even a nonsense name is refused as
    busy rather than reported as missing."""
    refusal = removal_refusal(
        busy=True, device_name="never-existed", known_devices=KNOWN
    )
    assert refusal is not None
    assert refusal.title == "Rig Busy"


def test_a_busy_refusal_is_informational_not_a_warning():
    """"Not now" is not the same as "that was wrong"."""
    refusal = removal_refusal(busy=True, device_name="EPC2001C", known_devices=KNOWN)
    assert refusal is not None and refusal.severity == "info"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_name_is_refused(blank):
    refusal = removal_refusal(busy=False, device_name=blank, known_devices=KNOWN)
    assert refusal is not None
    assert refusal.title == "No Device Selected"


def test_an_unknown_device_is_refused_rather_than_deleted():
    refusal = removal_refusal(
        busy=False, device_name="EPC2001", known_devices=KNOWN
    )
    assert refusal is not None
    assert refusal.title == "Device Not Found"


def test_surrounding_whitespace_does_not_hide_a_known_device():
    assert (
        removal_refusal(busy=False, device_name="  EPC2001C  ", known_devices=KNOWN)
        is None
    )


def test_nothing_is_removable_when_no_devices_exist():
    refusal = removal_refusal(busy=False, device_name="EPC2001C", known_devices=())
    assert refusal is not None
    assert refusal.title == "Device Not Found"


# -- what the operator is told -----------------------------------------------


def test_the_confirmation_names_the_device_and_says_it_is_permanent():
    title, message = removal_confirmation("EPC2001C")
    assert title == "Confirm Delete Device"
    assert "EPC2001C" in message
    assert "cannot be undone" in message


def test_the_confirmation_says_what_survives():
    """The operator's real question is whether the measurements are gone."""
    _title, message = removal_confirmation("EPC2001C")
    for survivor in ("screenshots", "reports", "Sheets"):
        assert survivor in message


def test_the_report_repeats_that_external_data_was_left_alone():
    title, message = removal_report("EPC2001C")
    assert title == "Device Removed"
    assert "EPC2001C" in message
    assert "unchanged" in message


# -- what gets selected afterwards -------------------------------------------


def test_the_first_remaining_device_is_selected():
    assert device_after_removal(["GS66508B", "EPC2001C"]) == "GS66508B"


def test_removing_the_last_device_selects_nothing():
    """Empty is the signal to clear every device-dependent view."""
    assert device_after_removal([]) == ""


# -- adding ------------------------------------------------------------------


def test_a_plain_name_is_accepted():
    assert validated_new_device_name("EPC2001C") == "EPC2001C"


def test_surrounding_whitespace_is_trimmed_rather_than_rejected():
    assert validated_new_device_name("  EPC2001C  ") == "EPC2001C"


@pytest.mark.parametrize("cancelled", [None, ""])
def test_dismissing_the_dialog_is_a_cancel_and_says_nothing(cancelled):
    assert validated_new_device_name(cancelled) is None


@pytest.mark.parametrize(
    "bad", ["EPC2001C/rev2", "../escape", "EPC*2001", "  ", "!!!"]
)
def test_an_unusable_name_is_reported_rather_than_silently_sanitised(bad):
    """The name becomes a directory and a Sheets tab. An operator who typed
    'EPC2001C/rev2' must not be left looking for 'EPC2001Crev2'."""
    with pytest.raises(InputRejected) as raised:
        validated_new_device_name(bad)
    assert raised.value.title == "Invalid Device Name"


@pytest.mark.parametrize("allowed", ["EPC 2001C", "EPC_2001C", "EPC-2001C"])
def test_spaces_underscores_and_hyphens_are_usable(allowed):
    assert validated_new_device_name(allowed) == allowed
