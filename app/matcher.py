"""Three-zone matching decision engine (docs/architecture.md §3.1).

    sim >= accept and top-2 margin ok  -> match
    sim >= accept but ambiguous margin -> buffer (secondary verification)
    reject <= sim < accept             -> buffer
    sim < reject                       -> reject

Embeddings are L2-normalized at creation, so cosine similarity is a dot product.
"""
from dataclasses import dataclass

import numpy as np


@dataclass
class Decision:
    outcome: str  # 'match' | 'buffer' | 'reject'
    employee_id: int | None
    similarity: float
    margin: float


class Matcher:
    def __init__(self, gallery, accept: float, reject: float, min_margin: float):
        """gallery: list of (employee_id, np.ndarray) — multiple rows per employee."""
        if reject >= accept:
            raise ValueError("reject_threshold must be below accept_threshold")
        self.gallery = gallery
        self.accept = accept
        self.reject = reject
        self.min_margin = min_margin

    def match(self, probe: np.ndarray) -> Decision:
        if not self.gallery:
            return Decision("reject", None, 0.0, 0.0)

        # max similarity per employee across their reference set (arch §2.5)
        best: dict[int, float] = {}
        for emp_id, vec in self.gallery:
            sim = float(np.dot(probe, vec))
            if sim > best.get(emp_id, -2.0):
                best[emp_id] = sim

        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top_sim = ranked[0]
        margin = top_sim - ranked[1][1] if len(ranked) > 1 else 1.0

        if top_sim >= self.accept:
            if margin >= self.min_margin:
                return Decision("match", top_id, top_sim, margin)
            return Decision("buffer", top_id, top_sim, margin)  # look-alike guard
        if top_sim >= self.reject:
            return Decision("buffer", top_id, top_sim, margin)
        return Decision("reject", None, top_sim, margin)
