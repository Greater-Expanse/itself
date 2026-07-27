# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Repository-local evaluation runners and versioned report contracts."""

from .reporting import (
    EVALUATION_REPORT_VERSION,
    EvaluationCheck,
    EvaluationEnvironment,
    EvaluationOutcome,
    EvaluationReport,
    EvaluationReportValidationError,
    EvaluationReportValidator,
    EvaluationStatus,
    build_evaluation_report,
    evaluation_run_id,
    render_evaluation_markdown,
    write_evaluation_report,
)

__all__ = [
    "EVALUATION_REPORT_VERSION",
    "EvaluationCheck",
    "EvaluationEnvironment",
    "EvaluationOutcome",
    "EvaluationReport",
    "EvaluationReportValidationError",
    "EvaluationReportValidator",
    "EvaluationStatus",
    "build_evaluation_report",
    "evaluation_run_id",
    "render_evaluation_markdown",
    "write_evaluation_report",
]
