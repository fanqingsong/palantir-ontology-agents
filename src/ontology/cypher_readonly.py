"""Validation boundary for LLM-generated read-only Cypher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.ontology.schema import RelationshipType

_COMMENTS = re.compile(r"//.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)
_STRINGS = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_WRITE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|"
    r"GRANT|DENY|REVOKE|ALTER|RENAME|TERMINATE|USE)\b",
    re.IGNORECASE,
)
_DANGEROUS_CALL = re.compile(r"\bCALL\b", re.IGNORECASE)
_UNBOUNDED_PATH = re.compile(
    r"\[[^\]]*\*(?!\s*\d+\s*\.\.\s*\d+)[^\]]*\]",
    re.IGNORECASE,
)
_REL_TYPES = re.compile(r"\[[^\]]*:(?P<types>[A-Z_]+(?:\|[A-Z_]+)*)")
_PARAM = re.compile(r"\$(\w+)")
_LIMIT = re.compile(r"\bLIMIT\s+(?:\$(\w+)|(\d+))\b", re.IGNORECASE)


class CypherValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CypherPolicy:
    max_hops: int = 6
    max_rows: int = 200


def validate_readonly_cypher(
    cypher: str,
    parameters: dict[str, Any] | None = None,
    policy: CypherPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or CypherPolicy()
    params = dict(parameters or {})
    if not cypher or not cypher.strip():
        raise CypherValidationError("Cypher is empty")
    cleaned = _COMMENTS.sub(" ", cypher).strip()
    if ";" in cleaned.rstrip(";"):
        raise CypherValidationError("Only one Cypher statement is allowed")
    cleaned = cleaned.rstrip(";").strip()
    if _STRINGS.search(cleaned):
        raise CypherValidationError("Literal strings are forbidden; use parameters")
    structural = _STRINGS.sub("''", cleaned)
    if not re.match(r"^(MATCH|OPTIONAL\s+MATCH|UNWIND|WITH)\b", structural, re.IGNORECASE):
        raise CypherValidationError("Query must start with a read-only clause")
    if _WRITE.search(structural):
        raise CypherValidationError("Write and administration clauses are forbidden")
    if _DANGEROUS_CALL.search(structural):
        raise CypherValidationError("Procedures are forbidden in agent-generated Cypher")
    if _UNBOUNDED_PATH.search(structural):
        raise CypherValidationError("Paths must have an explicit bounded range")

    for match in re.finditer(r"\*\s*(\d+)\s*\.\.\s*(\d+)", structural):
        if int(match.group(2)) > policy.max_hops:
            raise CypherValidationError(
                f"Path upper bound exceeds {policy.max_hops} hops"
            )

    allowed_rel_types = {item.value for item in RelationshipType}
    for match in _REL_TYPES.finditer(structural):
        unknown = set(match.group("types").split("|")) - allowed_rel_types
        if unknown:
            raise CypherValidationError(
                f"Unknown relationship type(s): {', '.join(sorted(unknown))}"
            )

    missing = set(_PARAM.findall(structural)) - set(params)
    if missing:
        raise CypherValidationError(
            f"Missing parameter(s): {', '.join(sorted(missing))}"
        )
    limit_match = _LIMIT.search(structural)
    if not limit_match:
        raise CypherValidationError("Every query must include LIMIT")
    if limit_match.group(1):
        name = limit_match.group(1)
        try:
            requested = int(params[name])
        except (TypeError, ValueError):
            raise CypherValidationError("LIMIT parameter must be an integer") from None
        params[name] = min(max(1, requested), policy.max_rows)
    else:
        requested = int(limit_match.group(2))
        if requested > policy.max_rows:
            raise CypherValidationError(f"LIMIT exceeds {policy.max_rows}")

    for key, value in params.items():
        if isinstance(value, list) and len(value) > 500:
            raise CypherValidationError(f"Parameter ${key} has too many values")
    return params
