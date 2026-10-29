import builtins

from review_engine.pdf_utils import _extract_pdf_text


def test_minimal_web_install_extracts_pdf_without_optional_backends(monkeypatch):
    import pymupdf
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 40), "EMNETS reproducibility: ordinary least squares")
        raw = document.tobytes()
    original = builtins.__import__
    def installed_only(name, *args, **kwargs):
        if name == "PyPDF2" or name.startswith("pdfminer"):
            raise ImportError("optional extraction backend is absent")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", installed_only)
    assert "ordinary least squares" in _extract_pdf_text(raw)
