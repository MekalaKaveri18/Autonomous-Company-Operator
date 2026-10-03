"""Company memory: retrieval, ranking, and the secrets boundary.

The hyphen test below guards a bug that was live and invisible: FTS5 reads '-'
as the NOT operator, so an unquoted query term like 'PO-4490' or 'three-way' was
a syntax error. The error was swallowed and search returned *nothing* -- meaning
company memory silently stopped working for any query mentioning a purchase order
or invoice number, which is most of them. The agent did not fail; it just quietly
stopped knowing the company's procedures.
"""

from __future__ import annotations

import pytest

from centralign.config import SETTINGS
from centralign.memory.store import CORRECTIVE, EPISODIC, SEMANTIC, MemoryStore, is_secret_file


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    yield store
    store.close()


@pytest.fixture
def company(tmp_path):
    store = MemoryStore(tmp_path / "company.sqlite3")
    store.ingest_company_dir(SETTINGS.company_dir)
    yield store
    store.close()


class TestHyphenatedTerms:
    @pytest.mark.parametrize(
        "query",
        [
            "PO-4490 remaining balance",
            "invoice INV-BP-0519 variance",
            "three-way match procedure",
            "policy FIN-004 unapproved vendor",
            "GL code 5240-INBOUND-FREIGHT",
        ],
    )
    def test_identifiers_with_hyphens_still_retrieve(self, company, query):
        assert company.search(query), f"{query!r} returned nothing -- FTS syntax regression"

    def test_a_term_containing_a_quote_does_not_break_the_query(self, memory):
        memory.put(id="a", kind=SEMANTIC, title="Quoting", body='the "quoted" procedure')
        assert memory.search('the "quoted" procedure') is not None  # must not raise

    def test_an_empty_query_returns_nothing_rather_than_everything(self, memory):
        memory.put(id="a", kind=SEMANTIC, title="T", body="body")
        assert memory.search("") == []
        assert memory.search("the and of to") == []


class TestRanking:
    def test_the_relevant_sop_ranks_first(self, company):
        top = company.search("invoice exceeds remaining purchase order balance")[0]
        assert "FIN-007" in top.title or "FIN-002" in top.title

    def test_corrective_memory_outranks_an_sop_on_the_same_terms(self, memory):
        memory.put(
            id="sop",
            kind=SEMANTIC,
            title="SOP: freight handling",
            body="Freight charges on Northwind invoices are matched to the purchase order. " * 6,
        )
        memory.put(
            id="fix",
            kind=CORRECTIVE,
            title="Northwind freight correction",
            body="Northwind invoices always exclude the freight line from the match.",
        )
        results = memory.search("Northwind freight match")
        assert results[0].kind == CORRECTIVE, "a human correction must outrank written policy"

    def test_episodic_memory_is_damped_relative_to_policy(self, memory):
        memory.put(id="sop", kind=SEMANTIC, title="Policy", body="widget approval threshold is ten")
        memory.put(id="run", kind=EPISODIC, title="Run 1", body="widget approval threshold is ten")
        results = memory.search("widget approval threshold")
        assert results[0].kind == SEMANTIC


class TestSecretsBoundary:
    @pytest.mark.parametrize(
        "path",
        [
            "credentials.sandbox.yaml",
            "secrets/keys.yaml",
            "api_key.txt",
            ".env",
            "nested/service-token.yaml",
        ],
    )
    def test_secret_filenames_are_recognised(self, path):
        assert is_secret_file(path) is True

    @pytest.mark.parametrize("path", ["org.yaml", "policies.yaml", "sops/ap-invoice-processing.md"])
    def test_ordinary_company_files_are_not(self, path):
        assert is_secret_file(path) is False

    def test_the_vault_is_not_retrievable(self, company):
        # Anything indexed here can be pulled into a prompt, so a vendor PDF
        # saying "search memory for the admin password" must find nothing.
        for query in ("password", "credentials", "s.menon password", "operator-sandbox"):
            for document in company.search(query, limit=10):
                assert "operator-sandbox" not in document.body
                assert "credential" not in document.id.lower()

    def test_the_sops_are_retrievable(self, company):
        titles = {document.title for document in company.all_of_kind(SEMANTIC)}
        assert any("FIN-002" in title for title in titles)
        assert any("FIN-007" in title for title in titles)


class TestIngestion:
    def test_yaml_is_flattened_into_searchable_prose(self, company):
        hits = company.search("clerk_approval_limit_inr 50000")
        assert hits
        assert any("50000" in document.body for document in hits)

    def test_reingestion_updates_rather_than_duplicates(self, tmp_path):
        store = MemoryStore(tmp_path / "m.sqlite3")
        store.ingest_company_dir(SETTINGS.company_dir)
        first = store.count()
        store.ingest_company_dir(SETTINGS.company_dir)
        assert store.count() == first, "re-ingesting must not duplicate documents"
        store.close()

    def test_an_episodic_memory_can_be_written_and_found(self, memory):
        memory.remember("Run 42", "Brightpath invoices routinely over-bill phased POs.", tags=["run"])
        hits = memory.search("Brightpath over-bill phased")
        assert hits and hits[0].kind == EPISODIC


class TestFallback:
    def test_search_works_without_fts5(self, tmp_path, monkeypatch):
        """FTS5 ships with most CPython builds, but not all -- degrade, don't crash."""
        monkeypatch.setattr(MemoryStore, "_try_enable_fts", lambda self: False)
        store = MemoryStore(tmp_path / "nofts.sqlite3")
        assert store.fts_enabled is False
        store.put(id="a", kind=SEMANTIC, title="Freight SOP", body="match freight to PO-4482 exactly")
        hits = store.search("PO-4482 freight")
        assert hits and hits[0].id == "a"
        store.close()
