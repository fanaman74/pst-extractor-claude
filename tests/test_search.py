import datetime as dt

from app.embeddings import HashEmbedder, to_pgvector
from app.search import SearchFilters, build_query, render_snippet


def test_hybrid_query_has_both_branches_and_index_friendly_knn():
    sql, params = build_query("hybrid", SearchFilters())
    assert "websearch_to_tsquery" in sql and "<=>" in sql
    # the ORDER BY distance + LIMIT must sit in a subquery so the HNSW index is usable
    assert "ORDER BY dist\n                LIMIT %(k)s" in sql
    assert params == {}


def test_keyword_mode_skips_vector_search():
    sql, _ = build_query("keyword", SearchFilters())
    assert "%(emb)s" not in sql


def test_filters_become_parameters():
    f = SearchFilters(archive_id="a", sender="Bob@X.com ", date_from=dt.date(2001, 1, 1),
                      date_to=dt.date(2001, 12, 31), source_type="ATTACHMENT")
    sql, params = build_query("hybrid", f)
    assert params["f_sender"] == "%bob@x.com%"
    assert params["f_to"] == dt.date(2002, 1, 1)  # inclusive end date
    assert sql.count("c.source_type = %(f_source)s") == 2
    assert f.active


def test_unknown_source_type_is_ignored():
    _, params = SearchFilters(source_type="DROP TABLE").sql()
    assert params == {}


def test_snippet_escaping():
    assert render_snippet("<b>x</b> \x02hit\x03") == "&lt;b&gt;x&lt;/b&gt; <mark>hit</mark>"


def test_hash_embedder_is_normalised_and_lexical():
    class S:
        embed_dim = 64
    e = HashEmbedder(S())
    a, b, c = e.embed_documents(["pipeline capacity", "pipeline capacity report", "birthday party"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert abs(dot(a, a) - 1) < 1e-9
    assert dot(a, b) > dot(a, c)
    assert to_pgvector([0.5, -1.0]) == "[0.5,-1]"
