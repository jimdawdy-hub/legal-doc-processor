"""Extract positioned text from PDFs with local OCR models on scanned pages."""
import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pymupdf


Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Word:
    text: str
    box: Box


@dataclass(frozen=True)
class Line:
    text: str
    box: Box
    words: tuple[Word, ...]
    confidence: float | None = None


@dataclass(frozen=True)
class Page:
    number: int
    width: float
    height: float
    source: str
    lines: tuple[Line, ...]


def _native_lines(page, words):
    grouped = {}
    for x0, y0, x1, y1, text, block, line, _ in words:
        rect = pymupdf.Rect(x0, y0, x1, y1) * page.rotation_matrix
        grouped.setdefault((block, line), []).append(Word(text, tuple(rect)))
    lines = []
    for words in grouped.values():
        rect = pymupdf.Rect(words[0].box)
        for word in words[1:]:
            rect |= pymupdf.Rect(word.box)
        lines.append(Line(' '.join(word.text for word in words), tuple(rect), tuple(words)))
    return tuple(lines)


def _requires_ocr(page, native_words):
    if not native_words:
        return True
    # A full-page scan may already have an incomplete or poor hidden text layer.
    bounds = page.rect * page.derotation_matrix
    return any((pymupdf.Rect(image['bbox']) & bounds).get_area() >= 0.70 * bounds.get_area()
               for image in page.get_image_info())


def _scale_line(line, x_scale, y_scale):
    def scale(box):
        x0, y0, x1, y1 = box
        return x0 * x_scale, y0 * y_scale, x1 * x_scale, y1 * y_scale

    return Line(line.text, scale(line.box), tuple(Word(w.text, scale(w.box)) for w in line.words),
                line.confidence)


def read_pages(path, recognize, *, dpi=300, shard=0, shards=1, numbers=None):
    """Yield lines and word boxes in displayed-page PDF points, with a top-left origin.

    The recognizer receives one rendered page and returns its boxes in image pixels.
    Shards are disjoint, so GPU workers write independent files.
    """
    if not 0 <= shard < shards or not 72 <= dpi <= 600:
        raise ValueError('Use a valid shard and a resolution from 72 to 600 DPI.')
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError('The PDF requires a password.')
        for index in range(shard, len(pdf), shards):
            number = index + 1
            if numbers is not None and number not in numbers:
                continue
            page = pdf[index]
            width, height = page.rect.width, page.rect.height
            words = page.get_text('words')
            if _requires_ocr(page, words):
                image = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=False)
                lines = tuple(_scale_line(line, width / image.width, height / image.height)
                              for line in recognize(image))
                yield Page(number, width, height, 'ocr', lines)
            else:
                yield Page(number, width, height, 'native', _native_lines(page, words))


class Paddle:
    """PaddleOCR text detection and word alignment with local ONNX models."""

    def __init__(self, detector, recognizer, *, device='gpu:0'):
        if any(not (folder / 'inference.onnx').is_file() for folder in (detector, recognizer)):
            raise ValueError('Provide local Paddle detector and recognizer ONNX snapshots.')
        os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                          HF_HUB_DISABLE_TELEMETRY='1', PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='1')
        import onnxruntime
        from paddleocr import PaddleOCR

        cuda = device.startswith('gpu')
        engine_config = {'intra_op_num_threads': 2, 'inter_op_num_threads': 1}
        if cuda:
            if 'CUDAExecutionProvider' not in onnxruntime.get_available_providers():
                raise RuntimeError('Install ONNX Runtime GPU with CUDA support.')
            onnxruntime.preload_dlls()
            engine_config.update(
                providers=['CUDAExecutionProvider', 'CPUExecutionProvider'],
                provider_options=[{'cudnn_conv_algo_search': 'HEURISTIC'}, {}],
            )
        self.engine = PaddleOCR(
            device=device, engine='onnxruntime', engine_config=engine_config,
            text_detection_model_name='PP-OCRv6_medium_det', text_detection_model_dir=str(detector),
            text_recognition_model_name='PP-OCRv6_medium_rec', text_recognition_model_dir=str(recognizer),
            text_recognition_batch_size=8,
            use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
            return_word_box=True, text_det_limit_side_len=3072, text_det_limit_type='max',
        )
        pipeline = self.engine.paddlex_pipeline
        if cuda and any('CUDAExecutionProvider' not in model.runner.session.get_providers()
                        for model in (pipeline.text_det_model, pipeline.text_rec_model)):
            raise RuntimeError('The OCR runtime did not initialize both models on CUDA.')

    def __call__(self, image):
        import numpy as np

        rgb = np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width, 3)
        result, = self.engine.predict(rgb[..., ::-1].copy())
        for text, box, confidence, words, word_boxes in zip(
            result['rec_texts'], result['rec_boxes'], result['rec_scores'],
            result['text_word'], result['text_word_boxes'], strict=True,
        ):
            positioned = tuple(Word(word, tuple(map(float, rect)))
                               for word, rect in zip(words, word_boxes, strict=True))
            yield Line(text, tuple(map(float, box)), positioned, float(confidence))


def write_pages(path, pages):
    """Publish a complete transcript to a new file in an owner-only directory."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.stat()
    if path.parent.is_symlink() or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError('The OCR output directory must be private to the current user.')
    if path.exists() or path.is_symlink():
        raise FileExistsError('Choose a new OCR output path.')
    totals = dict(pages=0, scanned_pages=0, empty_pages=0, words=0, lines_without_word_boxes=0)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            for page in pages:
                totals['pages'] += 1
                totals['scanned_pages'] += page.source == 'ocr'
                totals['empty_pages'] += not page.lines
                totals['words'] += sum(len(line.words) for line in page.lines)
                totals['lines_without_word_boxes'] += sum(not line.words for line in page.lines)
                stream.write(json.dumps(asdict(page), ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--detector', type=Path, required=True, help='Local Paddle detector snapshot')
    parser.add_argument('--recognizer', type=Path, required=True, help='Local Paddle recognizer snapshot')
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--shard', type=int, default=0, help='Zero-based worker index')
    parser.add_argument('--shards', type=int, default=1)
    args = parser.parse_args()
    pages = read_pages(args.input, Paddle(args.detector, args.recognizer), dpi=args.dpi,
                       shard=args.shard, shards=args.shards)
    print(json.dumps(write_pages(args.output, pages)))


if __name__ == '__main__':
    main()
