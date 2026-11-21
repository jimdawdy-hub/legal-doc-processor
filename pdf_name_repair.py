"""Correct confirmed name spellings in a synthetic scan PDF and verify both text layers."""
import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from PIL import Image, ImageChops, ImageDraw
import pymupdf

from medical_anonymize import BILLING, Edit, apply_edits, input_digest, read_transcript
from medical_dates import shift_dates
from patient_names import spaced_word
from pdf_ocr import Line
from pdf_review import ocr_passes
from pdf_synthetic import (
    Mask, PagePainter, _check_amount_pixels, _checks_on_pages, _date_regions, _line_masks,
    _painted_glyphs, _pixel_box, _private_parent, _scrub_search_aliases, _search_name_edits,
)


SCAN_PROFILES = ((300, 3), (360, 11), (315, 6))
NAME = re.compile(r"[^\W\d_]+(?:[ .’'-]+[^\W\d_]+)*\.?")
SUFFIX = r'(?:jr|sr|junior|senior|ii|iii|iv)\.?'


@dataclass(frozen=True)
class NameReplacement:
    old: str
    new: str

    def rule(self):
        exact = re.escape(self.old)
        spaced = '[ \t]+'.join(spaced_word(word) for word in self.old.split())
        return re.compile(rf'{exact}|(?<!\w){spaced}(?!\w)', re.I), self.match_case

    def match_case(self, match):
        if match[0].isupper():
            return self.new.upper()
        if match[0].islower():
            return self.new.lower()
        return self.new


def _duplicate_rules(canonical, replacements):
    """Keep the full name and match only extra copies of its given name for removal."""
    parts = canonical.split()
    if re.fullmatch(SUFFIX, parts[-1], re.I):
        parts.pop()
    if len(parts) < 2:
        raise ValueError('A canonical name needs a given name and surname.')
    first, *surname = parts
    alternatives = {first, *(item.old for item in replacements
                             if item.new.casefold() == first.casefold())}
    space = r'[ \t]+'
    separator = r'(?:[ \t]*,[ \t]*|[ \t]+)'
    given = '(?:' + '|'.join(space.join(map(spaced_word, value.split()))
                             for value in sorted(alternatives, key=lambda item: (-len(item), item))) + r')(?!\w)'
    family = space.join(map(spaced_word, surname)) + r'(?!\w)'
    full = given + separator + family
    return [
        (re.compile(rf'(?<!\w)(?P<extra>(?:{given}{separator})+)(?={full})', re.I), ''),
        (re.compile(rf'(?<!\w){full}(?:{space}{SUFFIX})?,?(?P<extra>(?:{space}{given}(?!{separator}{family}),?)+)',
                    re.I), ''),
    ]


def load_rules(path):
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, list) or not records:
        raise ValueError('Provide at least one private name replacement.')
    replacements = []
    canonical = []
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) not in ({'from', 'to'}, {'canonical'}) or not all(
            isinstance(value, str) and len(value) <= 120 and NAME.fullmatch(value)
            for value in row.values()
        ):
            raise ValueError('Provide either from and to names or a canonical full name.')
        if 'canonical' in row:
            canonical.append(row['canonical'])
            continue
        if row['from'].casefold() in seen:
            raise ValueError('Each source spelling must be unique.')
        seen.add(row['from'].casefold())
        replacements.append(NameReplacement(row['from'], row['to']))
    rules = [item.rule() for item in sorted(replacements, key=lambda item: -len(item.old))]
    if any(pattern.search(item.new) for pattern, _ in rules for item in replacements):
        raise ValueError('Replacement names must be free of the old spellings.')
    return [rule for name in canonical for rule in _duplicate_rules(name, replacements)] + rules


def _name_edits(text, rules):
    return [Edit(start, end, 'patient', new) for start, end, new in _search_name_edits(text, rules, ())]


def _same_printed_row(left, right):
    x0, y0, x1, y1 = left.box
    a0, b0, _, b1 = right.box
    height = max(y1 - y0, b1 - b0)
    return (x0 < a0 and -2 <= a0 - x1 <= max(8, min(20, 1.8 * height))
            and abs((y0 + y1) - (b0 + b1)) <= .9 * height)


def _next_word(words, used, start):
    left = words[start][0]
    _, top, end, bottom = left.box
    word = None
    nearest_x = None
    best = float('inf')
    for i in range(start + 1, len(words)):
        candidate = words[i][0]
        x, y, _, base = candidate.box
        if x > end + 20 or (nearest_x is not None and x > nearest_x + 2):
            break
        if i in used or not _same_printed_row(left, candidate):
            continue
        if nearest_x is None:
            nearest_x = x
        # Overlapping OCR readings can belong to different printed rows.
        score = abs((top + bottom) - (y + base)) / 2 + abs((bottom - top) - (base - y))
        if score < best:
            best, word = score, i
    return word


def _matching_lines(page, rules):
    if not any('extra' in pattern.groupindex for pattern, _ in rules):
        yield from page.lines
        return
    # OCR lines can contain words from different printed rows. Group by word position.
    yield from (line for line in page.lines if not line.words)
    words = sorted(((word, line.confidence) for line in page.lines for word in line.words),
                   key=lambda item: item[0].box[0])
    used = set()
    for i, item in enumerate(words):
        if i in used:
            continue
        group = [item]
        last = i
        while (next_word := _next_word(words, used, last)) is not None:
            group.append(words[next_word])
            used.add(next_word)
            last = next_word
        positioned = tuple(word for word, _ in group)
        bounds = pymupdf.Rect(positioned[0].box)
        for word in positioned[1:]:
            bounds |= pymupdf.Rect(word.box)
        yield Line(' '.join(word.text for word in positioned), tuple(bounds), positioned,
                   min(score if score is not None else .95 for _, score in group))


def _keep_other_rows(mask, line, page):
    box = pymupdf.Rect(mask.box)
    midpoint = (box.y0 + box.y1) / 2
    row = set(line.words)
    for source in page.lines:
        for word in source.words:
            if word in row:
                continue
            other = pymupdf.Rect(word.box)
            if (box & other).is_empty:
                continue
            if midpoint < other.y0 < box.y1 < other.y1:
                box.y1 = other.y0
            elif other.y0 < box.y0 < other.y1 < midpoint:
                box.y0 = other.y1
    return Mask(tuple(box), mask.replacement)


def _hits(pages, rules):
    for page in pages:
        for line in _matching_lines(page, rules):
            edits = _name_edits(line.text, rules)
            if edits:
                masks, fallback = _line_masks(line, edits)
                if fallback:
                    raise RuntimeError('A name match has no reliable word position.')
                for mask in masks:
                    if not mask.replacement:
                        mask = _keep_other_rows(mask, line, page)
                    yield mask, line.confidence if line.confidence is not None else .95


def _overlaps(a, b):
    a, b = pymupdf.Rect(a.box), pymupdf.Rect(b.box)
    return (a & b).get_area() >= .48 * min(a.get_area(), b.get_area())


def _join_hits(pages, rules):
    masks = []
    for mask, _ in sorted(_hits(pages, rules), key=lambda hit: -hit[1]):
        for i, prior in enumerate(masks):
            if _overlaps(mask, prior):
                a, b = pymupdf.Rect(prior.box), pymupdf.Rect(mask.box)
                if min(a.width, b.width) >= .63 * max(a.width, b.width):
                    masks[i] = Mask(tuple(a | b), prior.replacement)
                break
        else:
            masks.append(mask)
    return masks


def _read_image(pdf, page):
    images = page.get_images()
    if len(images) != 1:
        raise RuntimeError('Use a synthetic PDF with one full-page image per page.')
    xref = images[0][0]
    pixmap = pymupdf.Pixmap(pdf, xref)
    if pixmap.alpha or pixmap.n not in (1, 3):
        raise RuntimeError('Expected a grayscale or RGB synthetic page image.')
    return xref, Image.frombytes('L' if pixmap.n == 1 else 'RGB',
                                 (pixmap.width, pixmap.height), pixmap.samples)


def _replace_image(pdf, xref, image):
    # Replace the referenced object directly so the foreground and search streams stay separate.
    color = 'DeviceGray' if image.mode == 'L' else 'DeviceRGB'
    interpolate = pdf.xref_get_key(xref, 'Interpolate')[1] == 'true'
    pdf.update_object(xref, f'<< /Type /XObject /Subtype /Image /Width {image.width} '
                      f'/Height {image.height} /ColorSpace /{color} /BitsPerComponent 8 '
                      f'/Interpolate {str(interpolate).lower()} >>')
    pdf.update_stream(xref, image.tobytes(), compress=True)


def _source_ink_survives(raw, current, box):
    old = raw.crop(box).tobytes()
    new = current.crop(box).tobytes()
    ink = [i for i, value in enumerate(old) if value < 155]
    return (len(ink) >= 12 and sum(abs(old[i] - new[i]) <= 8 for i in ink) >= .94 * len(ink)
            and sum(abs(a - b) > 15 for a, b in zip(old, new)) <= .09 * len(old))


def _proof_from_source(reference, descriptions, current, candidates, rules, sx, sy):
    pending = [mask for mask, _ in _hits(descriptions, rules)
               if not any(_overlaps(mask, other) for other in candidates)]
    if not pending:
        return []
    pixmap = reference.get_pixmap(dpi=round(72 * sx), colorspace=pymupdf.csRGB, alpha=False, annots=False)
    if (pixmap.width, pixmap.height) != current.size:
        raise RuntimeError('The reference PDF does not align with the synthetic page.')
    image = Image.frombytes('RGB', current.size, pixmap.samples).convert('L')
    grayscale = current.convert('L')
    confirmed = []
    for mask in pending:
        if any(_overlaps(mask, other) for other in confirmed):
            continue
        if _source_ink_survives(image, grayscale, _pixel_box(mask.box, sx, sy, *current.size)):
            confirmed.append(mask)
    return confirmed


def _apply_masks(image, masks, pages, painter, sx, sy):
    painted = image.convert('RGB')
    protected = []
    for page in pages:
        _, nearby = _check_amount_pixels(painted, page.lines, masks, sx, sy)
        protected.extend(nearby)
        for _, _, box in _date_regions(page, 0):
            if any(not (box & pymupdf.Rect(mask.box)).is_empty for mask in masks):
                pixels = _pixel_box(box, sx, sy, *image.size)
                protected.append((pixels, painted.crop(pixels)))
    before = painted.copy()
    if painter.stamp(painted, masks, sx, sy):
        raise RuntimeError('A replacement would be too small to read.')
    if any(ImageChops.difference(prior, painted.crop(box)).getbbox() for box, prior in protected):
        raise RuntimeError('A proposed name replacement intersects a date or amount.')
    delta = ImageChops.difference(before, painted)
    draw = ImageDraw.Draw(delta)
    for mask in masks:
        left, top, right, bottom = _pixel_box(mask.box, sx, sy, *image.size)
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
    if delta.getbbox():
        raise RuntimeError('A proposed correction changes pixels outside the name.')
    return painted.convert(image.mode)


def _text_fingerprint(text):
    return (
        Counter(c for c in text if not c.isspace()),
        Counter(re.sub(r'[\s,$]', '', value) for value in BILLING.findall(text)),
        Counter(date.original for line in text.splitlines() for date in shift_dates(line, 0)[0]),
    )


def _check_pdf(pdf, scans=()):
    if pdf.needs_pass:
        raise ValueError('The synthetic PDF must be unlocked.')
    for pages in scans:
        _checks_on_pages(pdf, pages)
    if pdf.embfile_count() or pdf.get_toc() or pdf.get_xml_metadata():
        raise RuntimeError('Unexpected extra content in the synthetic PDF.')
    for page in pdf:
        images = page.get_image_info()
        if (page.rotation or len(images) != 1 or len(page.get_images()) != 1
                or any(abs(a - b) > .25 for a, b in zip(images[0]['bbox'], page.rect))
                or images[0]['transform'][0] <= 0 or images[0]['transform'][3] <= 0
                or abs(images[0]['transform'][1]) > .05 or abs(images[0]['transform'][2]) > .05
                or page.first_annot or page.first_widget or page.first_link):
            raise RuntimeError('Expected an upright synthetic scan without interactive PDF content.')


def _apply_corrections(pdf, scans, rules, font_path, reference, reference_scans):
    font = pymupdf.Font(fontfile=str(font_path))
    painter = PagePainter(font_path)
    image_refs = Counter(page.get_images()[0][0] for page in pdf)
    receipt = Counter(visible_replacements=0, searchable_replacements=0, reference_matches=0)
    changed = {}
    for index, page in enumerate(pdf):
        count, remaining = _scrub_search_aliases(page, rules, font)
        if remaining:
            raise RuntimeError('A name remains in the searchable or painted PDF text.')
        receipt['searchable_replacements'] += count
        current_scans = [pages[index] for pages in scans]
        masks = _join_hits(current_scans, rules)
        if not masks and reference is None:
            continue
        xref, image = _read_image(pdf, page)
        sx, sy = image.width / page.rect.width, image.height / page.rect.height
        if reference is not None:
            extra = _proof_from_source(reference[index], [pages[index] for pages in reference_scans],
                                       image, masks, rules, sx, sy)
            masks.extend(extra)
            receipt['reference_matches'] += len(extra)
        if masks:
            if image_refs[xref] != 1:
                raise RuntimeError('A page image needing a correction is shared by multiple pages.')
            updated = _apply_masks(image, masks, current_scans, painter, sx, sy)
            _replace_image(pdf, xref, updated)
            changed[index] = hashlib.sha256(updated.tobytes()).digest()
            receipt['visible_replacements'] += len(masks)
    return changed, receipt


def _verify(source, candidate, scans, rules, changed):
    with pymupdf.open(source) as before, pymupdf.open(candidate) as after:
        _check_pdf(after, scans)
        for index, (old, new) in enumerate(zip(before, after, strict=True)):
            original, corrected = old.get_text(), new.get_text()
            expected = apply_edits(original, _name_edits(original, rules))
            if (_name_edits(corrected, rules) or _text_fingerprint(expected) != _text_fingerprint(corrected)
                    or _painted_glyphs(old) != _painted_glyphs(new)):
                raise RuntimeError('Searchable or painted text differs beyond the requested name changes.')
            if index in changed:
                pixels = _read_image(after, new)[1].tobytes()
                if hashlib.sha256(pixels).digest() != changed[index]:
                    raise RuntimeError('The saved page image differs from the checked correction.')
            elif before.xref_stream_raw(old.get_images()[0][0]) != after.xref_stream_raw(new.get_images()[0][0]):
                raise RuntimeError('An untouched page image moved or changed.')


def repair(source, target, rules, *, font_path, workers, reference=None, reference_scans=()):
    _private_parent(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Choose a new PDF path for the correction.')
    digest = input_digest(source)
    reference_digest = input_digest(reference) if reference is not None else None
    with pymupdf.open(source) as pdf:
        _check_pdf(pdf)
    scans = tuple(ocr_passes(source, SCAN_PROFILES, workers))
    with NamedTemporaryFile(dir=target.parent, suffix='.pdf') as stage:
        with pymupdf.open(source) as pdf:
            _check_pdf(pdf, scans)
            if reference is None:
                changed, receipt = _apply_corrections(pdf, scans, rules, font_path, None, ())
            else:
                with pymupdf.open(reference) as original:
                    for pages in reference_scans:
                        _checks_on_pages(original, pages)
                        _checks_on_pages(pdf, pages)
                    changed, receipt = _apply_corrections(pdf, scans, rules, font_path, original, reference_scans)
            pdf.save(stage.name, garbage=4, deflate=True, use_objstms=1)
        _verify(source, stage.name, scans, rules, changed)
        for checked in ocr_passes(stage.name, SCAN_PROFILES, workers):
            if any(_name_edits(line.text, rules) for page in checked for line in _matching_lines(page, rules)):
                raise RuntimeError('A name remains in the corrected page images.')
        if digest != input_digest(source) or (reference is not None and reference_digest != input_digest(reference)):
            raise RuntimeError('An input PDF changed during name repair.')
        os.fsync(stage.fileno())
        os.link(stage.name, target)
    return dict(receipt, pages_checked=len(scans[0]), corrected_pages=len(changed), clean_ocr_passes=len(SCAN_PROFILES))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--replacements', required=True, type=Path, help='Private JSON list of name fixes')
    parser.add_argument('--font', required=True, type=Path, help='Local TrueType font')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--reference', type=Path, help='Optional original PDF for a pixel-level cross-check')
    parser.add_argument('--reference-transcript', type=Path, action='append', default=[])
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error('Use 1 to 32 OCR workers.')
    if bool(args.reference) != bool(args.reference_transcript):
        parser.error('Provide the reference PDF and at least one positioned reference transcript together.')
    reports = [list(read_transcript(path)) for path in args.reference_transcript]
    os.umask(0o077)
    receipt = repair(args.input, args.output, load_rules(args.replacements), font_path=args.font,
                     workers=args.workers, reference=args.reference, reference_scans=reports)
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
