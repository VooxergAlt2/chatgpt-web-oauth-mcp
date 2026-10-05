"""Adaptive routing primitives.

The routing package starts as a shadow-analysis layer. It must not change
delegate execution decisions until enough outcome data has been collected.
"""

from .task_shape import TaskShape, classify_task

__all__ = ["TaskShape", "classify_task"]
