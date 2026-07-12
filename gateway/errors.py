"""Stable gateway error categories and CLI exit codes."""

from __future__ import annotations


class GatewayError(Exception):
    exit_code = 1


class OperationalError(GatewayError):
    exit_code = 1


class ValidationError(GatewayError):
    exit_code = 2


class StateError(GatewayError):
    exit_code = 3


class ConflictError(GatewayError):
    exit_code = 4
