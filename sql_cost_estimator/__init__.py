"""SQL cost estimator and physical plan optimizer."""

from .estimator import estimate, estimate_file
from .models import EstimationResult, EstimatorInput

__version__ = "1.0.0"

__all__ = [
    "EstimationResult",
    "EstimatorInput",
    "estimate",
    "estimate_file",
]
