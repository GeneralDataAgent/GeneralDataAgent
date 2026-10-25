"""Human review storage used by the running service, not by offline evaluation.

The review stores were previously exported from ``generaldataagent.evaluation``,
which made the API import the offline evaluation package — including the pinned
stage-1/2/3 protocols — just to accept a review file path. Review *storage* is a
runtime port; review *acceptance* stays in ``evaluation``.
"""

from generaldataagent.review.local import DuplicateReviewError, LocalHumanReviewStore
from generaldataagent.review.scoped import ScopedHumanReviewStore

__all__ = ["DuplicateReviewError", "LocalHumanReviewStore", "ScopedHumanReviewStore"]
