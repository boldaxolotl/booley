"""Ticket board package — filesystem-based ticket management system.

Re-exports all public names so callers can do ``from ticket_board import X``.
"""

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------
from .analytics import (
    DEFAULT_PRICING as DEFAULT_PRICING,
)
from .analytics import (
    PRICING as PRICING,
)
from .analytics import (
    attribute_tokens_to_steps as attribute_tokens_to_steps,
)
from .analytics import (
    collect_all_messages as collect_all_messages,
)
from .analytics import (
    collect_step_transcript_usage as collect_step_transcript_usage,
)
from .analytics import (
    collect_step_usage as collect_step_usage,
)
from .analytics import (
    compute_cost_detailed as compute_cost_detailed,
)
from .analytics import (
    compute_step_cost as compute_step_cost,
)
from .analytics import (
    compute_step_durations as compute_step_durations,
)
from .analytics import (
    parse_transcript_usage as parse_transcript_usage,
)
from .analytics import (
    parse_transitions_log as parse_transitions_log,
)
from .analytics import (
    parse_usage_log as parse_usage_log,
)
from .analytics import (
    usage_entries_to_steps as usage_entries_to_steps,
)
from .archive import (
    op_archive as op_archive,
)

# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
from .cli import build_parser as build_parser
from .cli import main as main

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
from .constants import (
    DIR_STATUS_MAP as DIR_STATUS_MAP,
)
from .constants import (
    PRIORITY_ORDER as PRIORITY_ORDER,
)
from .constants import (
    REQUIRED_FIELDS as REQUIRED_FIELDS,
)
from .constants import (
    RUNTIME_FIELDS as RUNTIME_FIELDS,
)
from .constants import (
    STEP_ORDER as STEP_ORDER,
)
from .constants import (
    TICKET_DIRS as TICKET_DIRS,
)
from .constants import (
    VALID_PRIORITIES as VALID_PRIORITIES,
)
from .constants import (
    VALID_TYPES as VALID_TYPES,
)
from .constants import (
    normalize_dir as normalize_dir,
)
from .evidence import (
    op_collect_evidence as op_collect_evidence,
)

# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
from .execution import (
    classify_tickets as classify_tickets,
)
from .execution import (
    next_from_planned as next_from_planned,
)
from .execution import (
    resume_detect as resume_detect,
)
from .execution import (
    select_mutation_config as select_mutation_config,
)

# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------
from .frontmatter import (
    format_frontmatter as format_frontmatter,
)
from .frontmatter import (
    parse_frontmatter as parse_frontmatter,
)
from .frontmatter import (
    update_frontmatter as update_frontmatter,
)
from .helpers import (
    ensure_utf8_output as ensure_utf8_output,
)
from .helpers import (
    fmt_datetime_user as fmt_datetime_user,
)
from .helpers import (
    fmt_duration as fmt_duration,
)
from .helpers import (
    generate_slug as generate_slug,
)
from .helpers import (
    lock_fd as lock_fd,
)
from .helpers import (
    parse_arrow as parse_arrow,
)
from .helpers import (
    parse_iso as parse_iso,
)
from .helpers import (
    unlock_fd as unlock_fd,
)

# ---------------------------------------------------------------------------
# notifications (used internally by operations; no public re-exports needed)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# io
# ---------------------------------------------------------------------------
from .io import (
    TicketFileSpec as TicketFileSpec,
)
from .io import (
    TicketIO as TicketIO,
)
from .io import (
    find_ticket_file as find_ticket_file,
)
from .io import (
    scan_all_tickets as scan_all_tickets,
)

# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
from .logs import (
    PROGRESS_DEFAULTS as PROGRESS_DEFAULTS,
)
from .logs import (
    append_incident as append_incident,
)
from .logs import (
    clear_from_step as clear_from_step,
)
from .logs import (
    load_progress as load_progress,
)
from .logs import (
    progress_default as progress_default,
)
from .logs import (
    reset_progress as reset_progress,
)
from .logs import (
    save_progress as save_progress,
)

# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------
from .operations import (
    op_activate as op_activate,
)
from .operations import (
    op_approve as op_approve,
)
from .operations import (
    op_block as op_block,
)
from .operations import (
    op_board_move as op_board_move,
)
from .operations import (
    op_claim as op_claim,
)
from .operations import (
    op_complete as op_complete,
)
from .operations import (
    op_fail as op_fail,
)
from .operations import (
    op_handoff as op_handoff,
)
from .operations import (
    op_promote_waiting as op_promote_waiting,
)
from .operations import (
    op_reset as op_reset,
)
from .operations import (
    op_unblock as op_unblock,
)

# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
from .reporting import (
    display_board as display_board,
)
from .reporting import (
    format_step_detail as format_step_detail,
)
from .reporting import (
    format_timing_report as format_timing_report,
)
from .reporting import (
    format_usage_report as format_usage_report,
)

# ---------------------------------------------------------------------------
# run_metrics
# ---------------------------------------------------------------------------
from .run_metrics import (
    format_metrics_report as format_metrics_report,
)

# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
from .validation import (
    STEP_META_VALIDATORS as STEP_META_VALIDATORS,
)
from .validation import (
    format_validate_logs_report as format_validate_logs_report,
)
from .validation import (
    no_large_area_increase as no_large_area_increase,
)
from .validation import (
    no_unfixed_critical as no_unfixed_critical,
)
from .validation import (
    validate_logs as validate_logs,
)
from .validation import (
    validate_ticket_fields as validate_ticket_fields,
)
