from datetime import datetime

from services.mtp_date_normalizer import normalize_mtp_modify_date


def format_display_datetime(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def mtp_join(path: str, name: str) -> str:
    prefix = path.strip()
    separator = "" if prefix.endswith("|") else "|"
    return f"{prefix}{separator}{name}"


def format_adb_error(output: str) -> str:
    text = (output or "").strip()
    lower = text.lower()

    if "device offline" in lower:
        return (
            "ADB device is offline. Reconnect the RC-2 USB cable, unlock/confirm USB debugging "
            "on the RC-2, then verify with 'adb devices' until it shows state 'device'."
        )
    if "unauthorized" in lower:
        return (
            "ADB device is unauthorized. Accept the USB debugging authorization prompt on the RC-2 "
            "and retry."
        )
    if "no devices/emulators found" in lower:
        return (
            "No ADB device detected. Connect the RC-2 via USB, enable USB debugging, and retry."
        )
    return text
