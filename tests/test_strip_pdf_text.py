import fitz
import pytest
from strip_pdf_text import strip_text_layer


def _scan_like_pdf(path, text):
    """One page: a raster image plus an invisible text layer (like OCR'd scans)."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 100), False)
    pix.clear_with(255)
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_image(page.rect, pixmap=pix)
    page.insert_text((10, 50), text, fontsize=12, render_mode=3)
    page.add_text_annot((5, 5), "note SECRET")
    doc.set_metadata({"title": "Example record"})
    doc.save(str(path))


def test_removes_hidden_text_annotations_and_metadata(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _scan_like_pdf(src, "Zzyzx 555-0100")
    r = strip_text_layer(src, dst)
    assert r["chars_removed"] > 0 and r["chars_left"] == 0
    with fitz.open(str(dst)) as d:
        assert d[0].get_text().strip() == ""
        assert len(d[0].get_images()) == 1          # page image preserved
        assert not list(d[0].annots() or [])
        assert not d.metadata.get("title")
    assert b"Zzyzx" not in dst.read_bytes()
    assert (dst.stat().st_mode & 0o777) == 0o600


def test_input_untouched_and_same_path_refused(tmp_path):
    src = tmp_path / "in.pdf"
    _scan_like_pdf(src, "Zzyzx")
    before = src.read_bytes()
    with pytest.raises(ValueError):
        strip_text_layer(src, src)
    strip_text_layer(src, tmp_path / "out.pdf")
    assert src.read_bytes() == before
