"""Graph-coherence boosts for collective entity disambiguation."""

from __future__ import annotations

from src.entity_linking.models import EntityCandidate
from src.ontology.store import OntologyStore


def apply_graph_coherence(
    store: OntologyStore,
    candidate_sets: list[list[EntityCandidate]],
    max_candidates: int = 5,
) -> None:
    ids_by_set = [
        {candidate.entity_id for candidate in candidates[:max_candidates]}
        for candidates in candidate_sets
    ]
    for set_index, candidates in enumerate(candidate_sets):
        relevant_ids = set().union(*(
            ids for index, ids in enumerate(ids_by_set) if index != set_index
        )) if len(ids_by_set) > 1 else set()
        for candidate in candidates[:max_candidates]:
            connected = []
            try:
                for neighbor, rel in store.get_neighbors(candidate.entity_id):
                    if neighbor.id in relevant_ids:
                        connected.append({
                            "entity_id": neighbor.id,
                            "relationship": rel.relationship_type.value,
                        })
            except Exception:
                connected = []
            connected_ids = {item["entity_id"] for item in connected}
            for other_id in relevant_ids - connected_ids:
                try:
                    path = store.find_path(candidate.entity_id, other_id, max_hops=3)
                except Exception:
                    path = None
                if path and len(path) <= 4:
                    connected.append({
                        "entity_id": other_id,
                        "relationship": "PATH_" + str(len(path) - 1) + "_HOPS",
                    })
            candidate.graph_context = connected
            candidate.scores["coherence"] = min(1.0, len(connected) / 2)
