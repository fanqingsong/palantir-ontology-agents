"""Candidate score fusion and calibration."""

from __future__ import annotations

from src.entity_linking.models import EntityCandidate

_WEIGHTS = {
    "exact": 0.35,
    "fulltext": 0.15,
    "vector": 0.20,
    "fuzzy": 0.15,
    "type": 0.10,
    "coherence": 0.05,
}


def rerank(candidates: list[EntityCandidate]) -> list[EntityCandidate]:
    for candidate in candidates:
        candidate.score = round(sum(
            _WEIGHTS[key] * min(1.0, max(0.0, candidate.scores.get(key, 0.0)))
            for key in _WEIGHTS
        ), 4)
        if candidate.scores.get("exact", 0.0) >= 1.0 and candidate.scores.get("type", 0.0) > 0:
            candidate.score = max(candidate.score, 0.97)
    return sorted(candidates, key=lambda value: value.score, reverse=True)
