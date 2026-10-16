from app.chunking import chunk_text, normalize_text


def test_short_text_is_one_chunk():
    assert chunk_text("  hello \r\n\r\n\r\n\r\nworld  ") == ["hello\n\nworld"]


def test_empty_text_has_no_chunks():
    assert chunk_text("   \n\x00 ") == []


def test_long_text_is_split_with_overlap_and_full_coverage():
    words = [f"word{i}" for i in range(2000)]
    text = " ".join(words)
    chunks = chunk_text(text, size=500, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    seen = set(" ".join(chunks).split())
    assert seen == set(words)  # nothing lost
    # consecutive chunks share some words (overlap)
    assert set(chunks[0].split()) & set(chunks[1].split())


def test_prefers_paragraph_boundaries():
    para = "Sentence one is here. " * 15
    chunks = chunk_text(para + "\n\n" + para, size=400, overlap=0)
    assert chunks[0].endswith("here.")


def test_normalize_collapses_whitespace():
    assert normalize_text("a\t\t b\n\n\n\nc") == "a b\n\nc"
