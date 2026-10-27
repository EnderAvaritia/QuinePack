"""Expected failures, each mapped to a documented CLI exit code."""

from __future__ import annotations


class QuinePackError(Exception):
    """Base class for failures the CLI reports without a traceback."""

    exit_code = 1


class UsageError(QuinePackError):
    """Invalid command line arguments."""

    exit_code = 2


class VerificationError(QuinePackError):
    """Archive verification failed."""

    exit_code = 3


class PayloadError(QuinePackError):
    """Payload is missing, unsupported, or over the size limit."""

    exit_code = 4


class ExistsError(QuinePackError):
    """Target exists and the environment is not interactive."""

    exit_code = 5


class SevenZipMissingError(QuinePackError):
    """Verification needs an external 7-Zip executable."""

    exit_code = 6