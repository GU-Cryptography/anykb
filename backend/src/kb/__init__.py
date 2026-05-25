"""Knowledge Base (KB) module — M2.

A KB is a user-owned collection of documents that get parsed, chunked,
embedded and stored as vectors in a dedicated Qdrant collection
(`kb_{uuid}`). The KB is the unit of search: an Agent query at run-time
hits exactly one KB's collection.

Layout:
    models.py      KB + Document SQLAlchemy tables
    parsers/       One module per supported source type (md / pdf / docx / url)
    chunker.py     Text → chunks
    ingest.py      End-to-end pipeline (parse → chunk → embed → upsert)
    routes.py      HTTP CRUD endpoints
"""
