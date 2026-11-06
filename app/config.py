"""All settings come from environment variables (.env). No secrets in code."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _s(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name, str(default)))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # database
    database_url: str
    # mail transport
    smtp_user: str
    smtp_password: str
    smtp_host: str
    smtp_port: int
    imap_user: str
    imap_password: str
    imap_host: str
    imap_port: int
    email_dry_run: bool          # log emails instead of sending (safe demos)
    use_plus_addressing: bool    # Reply-To: agent+t<ID>@... (needs provider support)
    require_email_auth: bool     # leader-by-email requires SPF/DKIM pass
    mark_ignored_as_read: bool
    imap_fetch_limit: int
    imap_max_attempts: int
    send_acks: bool
    async_delivery: bool         # send assignment emails in the background (fast replies)
    # time / working hours
    timezone: str
    work_start_hour: int
    work_end_hour: int
    skip_weekends: bool
    # follow-up + escalation policy
    gap_far_hours: int           # due in > 7 days
    gap_mid_hours: int           # due in 2-7 days
    gap_near_hours: int          # due in <= 2 days
    gap_overdue_hours: int       # already overdue
    max_unanswered: int          # reminders with no reply before escalating
    overdue_escalate_after_hours: int
    # scheduler
    enable_scheduler: bool
    inbox_poll_seconds: int
    followup_job_minutes: int
    # app
    api_key: str
    auto_create_tables: bool
    agent_name: str
    brief_enabled: bool          # send the morning brief every working day
    brief_hour: int              # local hour to send it (0-23)
    brief_to: str                # optional override: comma-separated addresses. Empty = the top leader(s)
    dashboard_url: str           # optional link shown in the brief
    confirm_low_confidence: bool # ask the employee to confirm a status the AI was unsure about
    status_check_hours: int      # stop waiting for that answer after this many hours
    manual_step_minutes: int     # assumption behind the 'time saved' estimate: minutes one manual chase/update takes
    max_attachment_mb: int       # largest single file a leader can attach in the chat
    max_attachments: int         # most files on one chat message
    max_attachments_total_mb: int  # total per email (mail servers reject ~25 MB; base64 adds ~33%)
    attachment_orphan_hours: int # uploaded-but-never-used files are deleted after this long
    duplicate_window_minutes: int  # an identical assignment inside this window is treated as a repeat (0 = off)


def get_settings() -> Settings:
    smtp_user = _s("GMAIL_SMTP_USER")
    smtp_pass = _s("GMAIL_SMTP_PASSWORD")
    return Settings(
        database_url=_s("DATABASE_URL"),
        smtp_user=smtp_user,
        smtp_password=smtp_pass,
        smtp_host=_s("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_i("SMTP_PORT", 587),
        imap_user=_s("GMAIL_IMAP_USER", smtp_user),
        imap_password=_s("GMAIL_IMAP_PASSWORD", smtp_pass),
        imap_host=_s("IMAP_HOST", "imap.gmail.com"),
        imap_port=_i("IMAP_PORT", 993),
        email_dry_run=_b("EMAIL_DRY_RUN", False),
        use_plus_addressing=_b("USE_PLUS_ADDRESSING", False),
        require_email_auth=_b("REQUIRE_EMAIL_AUTH", False),
        mark_ignored_as_read=_b("MARK_IGNORED_AS_READ", False),
        imap_fetch_limit=_i("IMAP_FETCH_LIMIT", 20),
        imap_max_attempts=_i("IMAP_MAX_ATTEMPTS", 3),
        send_acks=_b("SEND_ACKS", True),
        async_delivery=_b("ASYNC_DELIVERY", True),
        timezone=_s("TIMEZONE", "Asia/Kolkata"),
        work_start_hour=_i("WORK_START_HOUR", 9),
        work_end_hour=_i("WORK_END_HOUR", 18),
        skip_weekends=_b("SKIP_WEEKENDS", True),
        gap_far_hours=_i("FOLLOWUP_GAP_FAR_HOURS", 72),
        gap_mid_hours=_i("FOLLOWUP_GAP_MID_HOURS", 48),
        gap_near_hours=_i("FOLLOWUP_GAP_NEAR_HOURS", 24),
        gap_overdue_hours=_i("FOLLOWUP_GAP_OVERDUE_HOURS", 24),
        max_unanswered=_i("MAX_UNANSWERED_REMINDERS", 3),
        overdue_escalate_after_hours=_i("OVERDUE_ESCALATE_AFTER_HOURS", 24),
        enable_scheduler=_b("ENABLE_SCHEDULER", True),
        inbox_poll_seconds=_i("INBOX_POLL_SECONDS", 60),
        followup_job_minutes=_i("FOLLOWUP_JOB_MINUTES", 15),
        api_key=_s("API_KEY"),
        auto_create_tables=_b("AUTO_CREATE_TABLES", True),
        agent_name=_s("AGENT_NAME", "Dhruva Work Agent"),
        brief_enabled=_b("BRIEF_ENABLED", True),
        brief_hour=_i("BRIEF_HOUR", 9),
        brief_to=_s("BRIEF_TO", ""),
        dashboard_url=_s("DASHBOARD_URL", ""),
        confirm_low_confidence=_b("CONFIRM_LOW_CONFIDENCE", True),
        status_check_hours=_i("STATUS_CHECK_HOURS", 24),
        manual_step_minutes=_i("MANUAL_STEP_MINUTES", 3),
        max_attachment_mb=_i("MAX_ATTACHMENT_MB", 10),
        max_attachments=_i("MAX_ATTACHMENTS", 5),
        max_attachments_total_mb=_i("MAX_ATTACHMENTS_TOTAL_MB", 15),
        attachment_orphan_hours=_i("ATTACHMENT_ORPHAN_HOURS", 24),
        duplicate_window_minutes=_i("DUPLICATE_WINDOW_MINUTES", 10),
    )
