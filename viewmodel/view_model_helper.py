from datetime import datetime


def format_display_datetime(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def normalize_mtp_modify_date(raw_value: str) -> str:
    raw = (raw_value or "").strip()
    if not raw:
        return ""

    if raw.startswith("12/30/1899") or raw.startswith("30/12/1899") or raw.startswith("1899-12-30"):
        return ""

    parse_formats = [
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in parse_formats:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue

        if parsed.year <= 1900:
            return ""
        return format_display_datetime(parsed)

    return raw


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
