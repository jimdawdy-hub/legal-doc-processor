import json
import stat

import pymupdf
import pytest

from pdf_ocr import Line, Page, Word, read_pages, write_pages


def test_mixed_pdf_uses_ocr_for_scans_with_or_without_invisible_text(tmp_path):
    source = tmp_path / 'mixed.pdf'
    pdf = pymupdf.open()
    text_page = pdf.new_page(width=100, height=200)
    text_page.insert_text((10, 30), 'Native text')
    text_page.set_rotation(90)
    raster = text_page.get_pixmap().tobytes('png')
    pdf.new_page(width=200, height=100).insert_image((0, 0, 200, 100), stream=raster)
    hidden_page = pdf.new_page(width=200, height=100)
    hidden_page.insert_image((0, 0, 200, 100), stream=raster)
    hidden_page.insert_text((10, 30), 'Stale OCR', render_mode=3)
    pdf.save(source)

    seen_sizes = []

    def recognize(pixmap):
        seen_sizes.append((pixmap.width, pixmap.height))
        box = (20., 20., 40., 40.)
        return [Line('Recognized', box, (Word('Recognized', box),), .9)]

    first = list(read_pages(source, recognize, dpi=144, shard=0, shards=2))
    second = list(read_pages(source, recognize, dpi=144, shard=1, shards=2))
    assert [(page.number, page.source) for page in first + second] == [(1, 'native'), (3, 'ocr'), (2, 'ocr')]
    assert first[0].lines[0].text == 'Native text'
    assert first[0].width == 200 and first[0].height == 100
    assert seen_sizes == [(400, 200), (400, 200)]
    assert first[1].lines[0].words[0].box == (10., 10., 20., 20.)
    assert second[0].lines[0].confidence == .9


def test_transcript_is_private_complete_and_preserves_existing_file(tmp_path):
    folder = tmp_path / 'ocr'
    output = folder / 'pages.jsonl'

    def broken_recognizer():
        yield Page(1, 100., 100., 'ocr', ())
        raise RuntimeError('recognition failed')

    with pytest.raises(RuntimeError):
        write_pages(output, broken_recognizer())
    assert not list(folder.iterdir())

    totals = write_pages(output, [Page(1, 100., 100., 'ocr', ())])
    assert totals['scanned_pages'] == totals['empty_pages'] == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())['number'] == 1
    with pytest.raises(FileExistsError):
        write_pages(output, [])
    assert json.loads(output.read_text())['number'] == 1
