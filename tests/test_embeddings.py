from agent.embeddings.faiss_index import FaissIndex


def test_faiss_add_and_search(tmp_path, monkeypatch):
    index_path = tmp_path / "index.faiss"
    meta_path = tmp_path / "meta.json"
    index = FaissIndex(index_path=index_path, metadata_path=meta_path)
    vectors = [[1.0, 0.0], [0.0, 1.0]]
    ids = ["a", "b"]
    index.add_vectors(vectors, ids)
    results = index.search([1.0, 0.1], k=1)
    assert results[0][0] == "a"
