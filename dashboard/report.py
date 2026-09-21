import datetime
from string import Template

TEMPLATE = Template(
    """🛣️ *Robolyze Daily Ops Update*
📅 *Today $date*

*📍 Location : _${location}_*
*🚛 Driver : _${driver}_*

*1. Data Sync & Cleanup*
⏸️ Sync script run - stoped
⏸️ recording/ - pause when needed or prompt

*2. System Health Check*
$sys_emoji System running
✅ Gating logic active (≥3 km/h trigger) — confirm manually, no sensor for this

*3. Camera Check*
$camera_insp_emoji Duromac feed inspected
$camera_clear_emoji $camera_clear_text

*4. Dashboard Update*
$dash_emoji Detections uploaded to client dashboard
✅ Confidence threshold set to $conf_value ✓

*5. Recording Status*
⏸️ Recording - pause when needed or prompt
$storage_emoji Pendrive's storage no longer full

Note:
▶️ = running
⏸️ = Pause
✅ = ok
🔴 = not ok
"""
)

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def format_report_date(date: datetime.date) -> str:
    day = date.day
    suffix = "th" if 11 <= day % 100 <= 13 else _ORDINAL_SUFFIXES.get(day % 10, "th")
    return f"{day}{suffix} {date.strftime('%B %Y (%A)')}"


def _ok(condition: bool) -> str:
    return "✅" if condition else "🔴"


def build_report(
    health: dict, default_confidence: str, date: datetime.date,
    town: str | None = None, driver_name: str | None = None,
) -> str:
    pipeline = health.get("pipeline") or {}
    upload = health.get("upload") or {}
    system = health.get("system") or {}
    clarity = (health.get("camera") or {}).get("clarity") or {}

    # "System running" in section 2 means the detection pipeline, not health.json's
    # `system` section (CPU/disk/throttle) — that one is only consulted for disk_alert
    # below. Intentional: the operator reads that line as "is it capturing".
    pipeline_ok = bool(pipeline.get("ok"))
    upload_ok = bool(upload.get("ok"))
    camera_inspected_ok = bool(clarity.get("checked"))
    camera_clear_ok = camera_inspected_ok and bool(clarity.get("clear"))
    if not camera_inspected_ok:
        camera_clear_text = "Camera feed clarity not checked"
    elif clarity.get("clear"):
        camera_clear_text = "Camera feed is clear"
    else:
        camera_clear_text = "Camera feed is NOT clear — check lens"
    storage_ok = not system.get("disk_alert", False)
    # `or`, not a .get default: the schema uses explicit null (see `error: null`), and a
    # .get default only fires when the key is absent — which rendered "set to None".
    conf_value = pipeline.get("confirmed_min_confidence") or default_confidence

    return TEMPLATE.substitute(
        date=format_report_date(date),
        location=town or "{Location}",
        driver=driver_name or "{Driver}",
        sys_emoji=_ok(pipeline_ok),
        dash_emoji=_ok(upload_ok),
        conf_value=conf_value,
        camera_insp_emoji=_ok(camera_inspected_ok),
        camera_clear_emoji=_ok(camera_clear_ok),
        camera_clear_text=camera_clear_text,
        storage_emoji=_ok(storage_ok),
    )
