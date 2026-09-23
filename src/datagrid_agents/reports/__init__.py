"""COR review report assets, renderer, and render-contract checks."""

from datagrid_agents.reports.render import (
    PayloadError,
    SCHEMA,
    build_data_island,
    render_report,
    status_classes,
)
from datagrid_agents.reports.validator import (
    CLOSING_LINE,
    PAYLOAD_PATH,
    SAMPLE_PATH,
    TEMPLATE_PATH,
    load_data_island,
    reconcile_data,
    style_block,
    validate_file,
    validate_report,
)

__all__ = [
    "CLOSING_LINE",
    "PAYLOAD_PATH",
    "PayloadError",
    "SAMPLE_PATH",
    "SCHEMA",
    "TEMPLATE_PATH",
    "build_data_island",
    "load_data_island",
    "reconcile_data",
    "render_report",
    "status_classes",
    "style_block",
    "validate_file",
    "validate_report",
]
