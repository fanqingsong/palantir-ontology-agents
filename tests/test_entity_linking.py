from src.entity_linking.models import LinkStatus, Mention
from src.entity_linking.normalizer import normalize_surface
from src.entity_linking.service import EntityLinkingService


def test_normalizes_multilingual_surface():
    assert normalize_surface("  ＴＳＭＣ，Inc.  ") == "tsmc inc"
    assert normalize_surface("台积电") == "台积电"


def test_links_seeded_chinese_alias(sample_ontology_store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = EntityLinkingService(sample_ontology_store)
    decisions = service.link([
        Mention(
            text="台积电",
            expected_types=["organization"],
            context="台积电先进制程供应链",
        )
    ])
    assert decisions[0].status == LinkStatus.LINKED
    assert decisions[0].entity_id == "tsmc"


def test_ambiguous_or_nil_does_not_create_entity(minimal_store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    before = minimal_store.entity_count
    decision = EntityLinkingService(minimal_store).link([
        Mention(text="Completely Unknown Object", expected_types=["organization"])
    ], mode="query")[0]
    assert decision.status in (LinkStatus.AMBIGUOUS, LinkStatus.NIL)
    assert minimal_store.entity_count == before


def test_persists_review_for_unresolved_mention(minimal_store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = EntityLinkingService(minimal_store)
    decision = service.link([
        Mention(text="Unknown", expected_types=["organization"], document_id="doc-1")
    ], mode="query", persist=True)[0]
    assert decision.persisted_mention_id is not None
    assert minimal_store.list_linking_reviews()


def test_review_approval_adds_verified_alias(minimal_store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = EntityLinkingService(minimal_store)
    service.link([
        Mention(text="Test Incorporated", expected_types=["organization"])
    ], mode="query", persist=True)
    review = minimal_store.list_linking_reviews()[0]
    minimal_store.resolve_linking_review(review["id"], "approved", "org1")
    assert "Test Incorporated" in minimal_store.aliases_for("org1")
