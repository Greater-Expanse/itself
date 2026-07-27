# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Static assertions for the experimental diagnostician boundary."""

from typing import assert_type

from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.diagnostician import (
    DiagnosisAssertion,
    DiagnosisRequest,
    DiagnosisResult,
    Diagnostician,
    DiagnosticianContractValidator,
    SuppliedResultDiagnostician,
)
from itself import JsonValue

adapter: Diagnostician = CacheKeyFixtureDiagnostician()


def check_diagnostician_types(
    diagnostician: Diagnostician,
    request: DiagnosisRequest,
) -> None:
    validator = DiagnosticianContractValidator()

    assert_type(diagnostician.diagnose(request), DiagnosisResult)
    assert_type(validator.invoke(diagnostician, request), DiagnosisResult)
    assert_type(validator.request_errors(request), list[str])


def check_diagnostician_decoding(
    validator: DiagnosticianContractValidator,
    request: DiagnosisRequest,
    value: JsonValue,
    result: DiagnosisResult,
) -> None:
    assert_type(validator.decode_assertion(request, value), DiagnosisAssertion)
    assert_type(validator.decode_result(request, value), DiagnosisResult)
    assert_type(SuppliedResultDiagnostician(result), SuppliedResultDiagnostician)
