class EstimatorError(Exception):
    """Base exception for user-facing estimator errors."""


class InputValidationError(EstimatorError):
    """Raised when the JSON schema statistics are invalid."""


class SQLParseError(EstimatorError):
    """Raised when SQL is outside the supported grammar."""


class OptimizationError(EstimatorError):
    """Raised when no valid physical plan can be produced."""
