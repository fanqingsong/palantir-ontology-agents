import pytest

from src.ontology.cypher_readonly import (
    CypherValidationError,
    validate_readonly_cypher,
)


def test_accepts_parameterized_bounded_read():
    params = validate_readonly_cypher(
        "MATCH p=(a:Entity)-[:DEPENDS_ON*1..3]-(b:Entity) "
        "WHERE a.id = $id RETURN p LIMIT $limit",
        {"id": "apple", "limit": 500},
    )
    assert params["limit"] == 200


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DELETE n RETURN n LIMIT 1",
        "MATCH (n) SET n.name = 'x' RETURN n LIMIT 1",
        "CALL db.labels() YIELD label RETURN label LIMIT 10",
        "MATCH p=(a)-[*]-(b) RETURN p LIMIT 10",
        "MATCH p=(a)-[r*]-(b) RETURN p LIMIT 10",
        "MATCH (n) RETURN n",
        "MATCH (n) RETURN n LIMIT 1; MATCH (m) RETURN m LIMIT 1",
    ],
)
def test_rejects_unsafe_queries(query):
    with pytest.raises(CypherValidationError):
        validate_readonly_cypher(query)


def test_rejects_unknown_relationship_type():
    with pytest.raises(CypherValidationError, match="Unknown relationship"):
        validate_readonly_cypher(
            "MATCH (a)-[:INVENTED_REL]->(b) RETURN a, b LIMIT 10"
        )


def test_rejects_keywords_hidden_by_comments_or_dollar_quotes():
    with pytest.raises(CypherValidationError):
        validate_readonly_cypher("CR/**/EATE (n) RETURN n LIMIT 1")
    with pytest.raises(CypherValidationError):
        validate_readonly_cypher(
            "MATCH (n) CA/**/LL db.labels() YIELD label RETURN label LIMIT 1"
        )
    with pytest.raises(CypherValidationError):
        validate_readonly_cypher("MATCH (n) WHERE n.name = $$secret$$ RETURN n LIMIT 1")


def test_allows_comments_in_a_read_query():
    validate_readonly_cypher(
        "MATCH (n:Entity) /* focus */ WHERE n.id = $id RETURN n LIMIT 5",
        {"id": "tsmc"},
    )


def test_keywords_inside_strings_are_not_treated_as_clauses():
    validate_readonly_cypher(
        "MATCH (n:Entity) WHERE n.name = $name RETURN n LIMIT 1",
        {"name": "DELETE"},
    )
