"""Create a new layout-preserving PDF containing synthetic medical identifiers."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
from tempfile import NamedTemporaryFile

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFont

from medical_anonymize import (
    BILLING, PEOPLE, ROLES, Candidate, Edit, _choose, _tighten, alias_expression, apply_edits,
    input_digest, make_plan, patient_surname, read_model_candidates, read_transcript,
)
from medical_dates import calendar_spans, shift_dates
from patient_names import patient_names
from pdf_ocr import Line, Word


@dataclass(frozen=True)
class Mask:
    box: tuple[float, float, float, float]
    replacement: str


def _character_to_word(line):
    left = [(char, i) for i, source in enumerate(line.text) if not source.isspace()
            for char in source.casefold()]
    right = [(char, i) for i, word in enumerate(line.words) for source in word.text
             if not source.isspace() for char in source.casefold()]
    text, words = (''.join(char for char, _ in side) for side in (left, right))
    if text == words:
        ranges = [(0, 0, len(text))]
    else:
        ranges = SequenceMatcher(None, text, words, autojunk=False).get_matching_blocks()
    return {left[i + n][1]: right[j + n][1] for i, j, count in ranges for n in range(count)}


def _line_masks(line, edits):
    aligned = _character_to_word(line)
    groups = []
    for edit in edits:
        chars = range(edit.start, edit.end)
        words = [aligned[i] for i in chars if i in aligned]
        if not words or any(i not in aligned and line.text[i].isalnum() for i in chars):
            return [Mask(line.box, apply_edits(line.text, edits))], True
        first, last = min(words), max(words)
        if groups and first <= groups[-1][1]:
            # A redacted value can share a printed word or glyph group with its label.
            groups[-1][1] = max(last, groups[-1][1])
            groups[-1][2].append(edit)
        else:
            groups.append([first, last, [edit]])

    masks = []
    for first, last, same_word in groups:
        boxes = [pymupdf.Rect(word.box) for word in line.words[first:last + 1]]
        if any(rect.is_empty or not rect.is_valid for rect in boxes):
            return [Mask(line.box, apply_edits(line.text, edits))], True
        chosen = [i for i, w in aligned.items() if first <= w <= last]
        begin = min(min(chosen), same_word[0].start)
        end = max(max(chosen) + 1, same_word[-1].end)
        box = pymupdf.Rect(boxes[0])
        for next_box in boxes[1:]:
            box |= next_box
        left = line.words[first - 1].box[2] if first else box.x0 - 2
        right = line.words[last + 1].box[0] if last + 1 < len(line.words) else box.x1 + 2
        # Cover antialiased edges while leaving a neighboring field's ink alone.
        x0 = box.x0 - max(.2, min(.85, (box.x0 - left) * .46))
        x1 = box.x1 + max(.2, min(.85, (right - box.x1) * .46))
        top = box.y0 - min(.85, box.height * .13)
        bottom = box.y1 + min(.85, box.height * .13)
        masks.append(Mask((x0, top, x1, bottom), apply_edits(line.text, same_word, begin=begin, end=end)))
    return masks, False


def _date_regions(page, days):
    for index, line in enumerate(page.lines):
        values, _ = shift_dates(line.text, days)
        for value in values:
            masks, fallback = _line_masks(line, [Edit(value.start, value.end, 'date', value.replacement)])
            if not fallback:
                yield index, value, pymupdf.Rect(masks[0].box)


def reconcile_dates(pages, independent, days):
    """Repair a missed date only where a second OCR reading matches its source position and digits."""
    if [page.number for page in pages] != [page.number for page in independent]:
        raise ValueError('Independent OCR must cover the source pages in order.')
    for page, other in zip(pages, independent, strict=True):
        if abs(page.width - other.width) > .05 or abs(page.height - other.height) > .05:
            raise ValueError('Independent OCR geometry must match the source.')
        existing = [box for _, _, box in _date_regions(page, days)]
        for index, value, box in _date_regions(other, days):
            if (other.lines[index].confidence or 0) < .60:
                continue
            if any((box & prior).get_area() >= .30 * min(box.get_area(), prior.get_area())
                   for prior in existing):
                continue
            source = other.lines[index].text[value.start:value.end]
            digits = re.sub(r'\D', '', source)
            if len(digits) < 5:
                continue
            matches = []
            for i, line in enumerate(page.lines):
                if (pymupdf.Rect(line.box) & box).is_empty:
                    continue
                word_boxes = [pymupdf.Rect(word.box) for word in line.words]
                overlapping = {w for w, rect in enumerate(word_boxes)
                               if (rect & box).get_area() >= .4 * rect.get_area()}
                if not overlapping:
                    continue
                positions = [c for c, w in _character_to_word(line).items() if w in overlapping]
                if not positions:
                    continue
                begin, end = min(positions), max(positions) + 1
                raw = line.text[begin:end]
                if '$' in raw or BILLING.search(raw):
                    continue
                for piece in re.finditer(r'[0-9OolI][0-9OolI\s/.\-–:\]]{4,24}', raw):
                    text = piece.group().rstrip(' \t/.-–:')
                    corrected = text.translate(str.maketrans('OoIl', '0011'))
                    actual = re.sub(r'\D', '', corrected)
                    similarity = SequenceMatcher(None, digits, actual, autojunk=False).ratio()
                    if len(actual) >= 5 and similarity >= .80:
                        start = begin + piece.start()
                        matches.append((similarity, i, start, start + len(text)))
            if not matches:
                continue
            score, i, start, end = max(matches)
            if sum(entry[0] == score for entry in matches) != 1:
                continue
            existing.append(box)
            yield (page.number, i), replace(value, start=start, end=end)


def load_repairs(path, pages, days):
    """Apply locally reviewed source positions and date readings to the first-pass transcript."""
    explicit_dates = []
    extra_candidates = []
    if path is None:
        return pages, explicit_dates, extra_candidates
    by_page = defaultdict(lambda: defaultdict(list))
    new_lines = defaultdict(list)
    geoms = {page.number: page for page in pages}
    for row in json.loads(path.read_text(encoding='utf-8')):
        number, index, start, end, role, box, reading = row
        page = geoms[number]
        rect = pymupdf.Rect(box) & pymupdf.Rect(0, 0, page.width, page.height)
        if role not in ROLES or rect.is_empty:
            raise ValueError('Invalid local repair coordinates or role.')
        if index == -1:
            # A second engine can read a region the first engine missed entirely.
            if not isinstance(reading, str) or not reading.strip() or len(reading) > 120:
                raise ValueError('Invalid supplemental reading.')
            if role == 'date':
                dates, _ = shift_dates(reading, days)
                if len(dates) != 1 or (dates[0].start, dates[0].end) != (0, len(reading)):
                    raise ValueError('A supplemental date reading must be one complete calendar date.')
            else:
                new_index = len(page.lines) + len(new_lines[number])
                proposed = _tighten(reading, Candidate(0, len(reading), role, 109))
                if not _choose(reading, [proposed]):
                    continue
                extra_candidates.append(((number, new_index), proposed))
            coords = tuple(rect)
            new_lines[number].append(Line(reading, coords, (Word(reading, coords),)))
            continue
        if not 0 <= index < len(page.lines) or not 0 <= start < end <= len(page.lines[index].text):
            raise ValueError('Local repair positions must match the input transcript.')
        if role == 'date':
            dates, _ = shift_dates(reading, days)
            if len(dates) != 1:
                raise ValueError('A local date repair must have one complete calendar date.')
            explicit_dates.append(((number, index), replace(dates[0], start=start, end=end)))
        else:
            extra_candidates.append(((number, index), Candidate(start, end, role, 109)))
        by_page[number][index].append((start, end, rect))
    updated = []
    for page in pages:
        if page.number not in by_page and page.number not in new_lines:
            updated.append(page)
            continue
        lines = list(page.lines)
        for index, changes in by_page[page.number].items():
            line = lines[index]
            aligned = _character_to_word(line)
            words = list(line.words)
            line_box = pymupdf.Rect(line.box)
            for start, end, rect in changes:
                positions = sorted({aligned[i] for i in range(start, end) if i in aligned})
                if not positions:
                    continue
                # Joining the outer edge boxes preserves interior word alignment.
                first, last = positions[0], positions[-1]
                head = pymupdf.Rect(words[first].box)
                tail = pymupdf.Rect(words[last].box)
                head.x0, tail.x1 = min(head.x0, rect.x0), max(tail.x1, rect.x1)
                for w in positions:
                    old = pymupdf.Rect(words[w].box)
                    old.y0, old.y1 = min(old.y0, rect.y0), max(old.y1, rect.y1)
                    if w == first:
                        old.x0 = head.x0
                    if w == last:
                        old.x1 = tail.x1
                    words[w] = replace(words[w], box=tuple(old))
                line_box |= rect
            lines[index] = replace(line, box=tuple(line_box), words=tuple(words))
        updated.append(replace(page, lines=tuple(lines) + tuple(new_lines[page.number])))
    return updated, explicit_dates, extra_candidates


def _pixel_box(box, sx, sy, width, height):
    left, top, right, bottom = box
    return (max(0, math.floor(left * sx)), max(0, math.floor(top * sy)),
            min(width, math.ceil(right * sx)), min(height, math.ceil(bottom * sy)))


def _background(image, box):
    left, top, right, bottom = box
    strips = [(left, max(0, top - 4), right, top),
              (left, bottom, right, min(image.height, bottom + 4))]
    colors = []
    for strip in strips:
        if strip[1] < strip[3]:
            sample = image.crop(strip)
            sample.thumbnail((80, 4))
            # Select a high-luminance sample to avoid using nearby glyphs as background.
            rgb = sample.tobytes()
            colors.extend(rgb[i:i + 3] for i in range(0, len(rgb), 3))
    return tuple(sorted(colors, key=sum)[3 * len(colors) // 4]) if colors else (255, 255, 255)


class PagePainter:
    def __init__(self, font):
        self.font = font
        self.sizes = {}

    def _font(self, size):
        if size not in self.sizes:
            self.sizes[size] = ImageFont.truetype(str(self.font), size)
        return self.sizes[size]

    def stamp(self, image, masks, sx, sy):
        draw = ImageDraw.Draw(image)
        positions = []
        small = 0
        for mask in masks:
            box = _pixel_box(mask.box, sx, sy, image.width, image.height)
            left, top, right, bottom = box
            if left >= right or top >= bottom:
                raise ValueError('A planned replacement has no visible area.')
            color = _background(image, box)
            positions.append((mask.replacement, box, color))
        for _, (left, top, right, bottom), color in positions:
            draw.rectangle((left, top, right - 1, bottom - 1), fill=color)
        for value, (left, top, right, bottom), color in positions:
            if not value:
                continue
            # The font is scaled to the original glyph height. A long replacement
            # is compressed horizontally to fit the field without covering other ink.
            value = ' '.join(value.split())
            size = max(5, int((bottom - top - 1) / .86))
            font = self._font(size)
            a, b, c, d = font.getbbox(value)
            while d - b > bottom - top and size > 5:
                size -= 1
                font = self._font(size)
                a, b, c, d = font.getbbox(value)
            if c - a > right - left:
                scale = max(.72, (right - left) / (c - a))
                size = max(5, int(size * scale))
                font = self._font(size)
                a, b, c, d = font.getbbox(value)
            w, h = max(1, c - a), max(1, d - b)
            target = max(1, min(w, right - left - 1))
            if h / sy < 3 or target < .55 * w:
                small += 1
            glyphs = Image.new('L', (w, h), 0)
            ImageDraw.Draw(glyphs).text((-a, -b), value, font=font, fill=255)
            if target < w:
                glyphs = glyphs.resize((target, h), Image.Resampling.LANCZOS)
            ink = (20, 20, 20) if sum(color) >= 3 * 145 else (248, 248, 248)
            image.paste(ink, (left, top + max(0, (bottom - top - h) // 2)), glyphs)
        return small


def _barcode_masks(image, sx, sy):
    import zxingcpp

    for code in zxingcpp.read_barcodes(image):
        vertices = [getattr(code.position, corner) for corner in
                    ('top_left', 'top_right', 'bottom_left', 'bottom_right')]
        xs = [point.x / sx for point in vertices]
        ys = [point.y / sy for point in vertices]
        x0, y0, x1, y1 = min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2
        if max(ys) - min(ys) <= 2.5 and max(xs) - min(xs) > 10:
            # For a scanline result, follow the visible stripes up and down to
            # cover the whole barcode while preserving nearby printed labels.
            left, top, right, bottom = _pixel_box(
                (min(xs), min(ys) - 45, max(xs), max(ys) + 45), sx, sy, image.width, image.height)
            gray = image.crop((left, top, right, bottom)).convert('L')
            columns = gray.width
            rows = gray.tobytes()
            middle = min(gray.height - 1, max(0, round((min(ys) + max(ys)) * .5 * sy) - top))
            seed = rows[middle * columns:(middle + 1) * columns]
            dark = [c for c, value in enumerate(seed) if value < 125]
            if dark:
                start, end = middle, middle
                for position in range(middle - 1, -1, -1):
                    if sum(rows[position * columns + c] < 160 for c in dark) < .72 * len(dark):
                        break
                    start = position
                for position in range(middle + 1, gray.height):
                    if sum(rows[position * columns + c] < 160 for c in dark) < .72 * len(dark):
                        break
                    end = position
                y0, y1 = min(y0, (top + start) / sy - 2), max(y1, (top + end + 1) / sy + 2)
        yield Mask((x0, y0, x1, y1), '')


def _search_layer(page, texts, font):
    writer = pymupdf.TextWriter(page.rect)
    for line, value in texts:
        rect = pymupdf.Rect(line.box) & page.rect
        value = ''.join(c if c.isprintable() else ' ' for c in value).strip()
        if rect.is_empty or not value:
            continue
        value += ' '
        length = font.text_length(value, fontsize=1)
        if length <= 0:
            continue
        size = min(rect.height * .84, (rect.width - .05) / length)
        if size > 0:
            writer.append((rect.x0, rect.y1 - rect.height * .16), value, font=font, fontsize=size)
    writer.write_text(page, render_mode=3)


def _search_aliases(spellings, registry):
    rules = []
    for aliases in spellings:
        role = aliases['role']
        if role not in PEOPLE or (role == 'patient' and 'identity' in aliases):
            continue
        variants = []
        for value in aliases['values']:
            if role != 'patient' and len(re.findall(r'[^\W_]+', value)) < 2 and len(value) < 10:
                continue
            if expression := alias_expression(value):
                variants.append(expression)
        if variants:
            pattern = re.compile(r'(?<!\w)(?:' + '|'.join(variants) + r')(?!\w)', re.I)
            rules.append((pattern, registry.display(aliases['values'][0], role)))
        if (surname := patient_surname(aliases)) and (expression := alias_expression(surname)):
            rules.append((re.compile(r'(?<!\w)' + expression + r'(?!\w)', re.I),
                          registry.display(surname, 'patient')))
    return rules


def _search_name_edits(text, rules, patients):
    candidates = [(span.start, span.end, patient.replacement)
                  for patient in patients
                  for span in patient.spans(text, include_synthetic=True, across_lines=True)
                  if not span.canonical]
    candidates.extend((hit.start(), hit.end(), value(hit) if callable(value) else value)
                      for pattern, value in rules for hit in pattern.finditer(text))
    if not candidates:
        return []
    dates = calendar_spans(text)
    accepted = []
    for start, end, value in candidates:
        if any(a <= start and end <= b for a, b in dates):
            continue
        if not any(a < end and start < b for a, b, _ in accepted):
            accepted.append((start, end, value))
    return sorted(accepted)


def _painted_glyphs(page):
    return Counter((span['type'], span['font'], span['dir'], span['size'], span['color'], char[0], char[2])
                   for span in page.get_texttrace() if span['type'] != 3 for char in span['chars'])


def _resource_key(page, group, name):
    pdf, owner, prefix = page.parent, page.xref, ''
    for part in ('Resources', group):
        path = prefix + part
        kind, value = pdf.xref_get_key(owner, path)
        if kind == 'xref':
            owner, prefix = int(value.split()[0]), ''
        else:
            prefix = path + '/'
    return owner, prefix + name


def _scrub_search_aliases(page, rules, font, patients=()):
    if (not rules and not patients) or not _search_name_edits(page.get_text(), rules, patients):
        return 0, 0
    printed = _painted_glyphs(page)
    if printed:
        pdf = page.parent
        painted, background, used = [], [], set()
        for ref in page.get_contents():
            content = pdf.xref_stream(ref).strip()
            modes = set(re.findall(rb'\b([0-7])\s+Tr\b', content))
            if re.search(rb'\bBT\b', content) and modes != {b'3'}:
                if b'3' in modes or not (content.startswith(b'q') and content.endswith(b'Q')):
                    raise RuntimeError('Painted and searchable text share a PDF content stream.')
                painted.append(ref)
                for group, pattern in (
                    ('Font', rb'/([-\w.]+)\s+[+.\d-]+\s+Tf\b'),
                    ('ExtGState', rb'/([-\w.]+)\s+gs\b'),
                ):
                    used.update((group, name.decode('ascii')) for name in re.findall(pattern, content))
            elif painted and content and (modes != {b'3'} or re.search(
                rb'(?<!\S)(?:Do|sh|S|s|f\*?|F|B\*?|b\*?|BI)(?!\S)', content
            )):
                raise RuntimeError('Painted text is interleaved with the PDF background.')
            elif content:
                background.append(ref)
        if not painted or not background:
            raise RuntimeError('Could not isolate the searchable PDF text.')

        def set_contents(refs):
            pdf.xref_set_key(page.xref, 'Contents', '[' + ' '.join(f'{ref} 0 R' for ref in refs) + ']')

        # Keep the foreground out of the redaction engine. Rectangles can intersect both layers.
        resources = {key: pdf.xref_get_key(*_resource_key(page, *key)) for key in sorted(used)}
        set_contents(background)
        try:
            count = _erase_search_matches(page, rules, font, patients, resources=resources)
        finally:
            set_contents([*page.get_contents(), *painted])
        if _painted_glyphs(page) != printed:
            raise RuntimeError('Printed PDF text changed during searchable text repair.')
    else:
        count = _erase_search_matches(page, rules, font, patients)
    return count, len(_search_name_edits(page.get_text(), rules, patients))


def _erase_search_matches(page, rules, font, patients, *, resources=()):
    # Remove the matched glyphs, not the full height of a search rectangle,
    # which can touch the next row of a tightly spaced table.
    characters = []
    positions = []
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text('rawdict', flags=flags)['blocks']:
        for line in block.get('lines', []):
            line_index = len(positions)
            for span in line['spans']:
                for char in span['chars']:
                    text = char['c']
                    characters.append(text)
                    positions.extend([(line_index, char['bbox'])] * len(text))
            characters.append('\n')
            positions.append(None)

    plain = ''.join(characters)
    regions = set()
    replacements = {}
    for start, end, replacement in _search_name_edits(plain, rules, patients):
        first_line = None
        replacement_box = pymupdf.Rect()
        for i in range(start, end):
            if plain[i].isspace() or positions[i] is None:
                continue
            line_index, bbox = positions[i]
            rect = pymupdf.Rect(bbox)
            if rect.is_empty:
                continue
            regions.add((rect.x0 + .20 * rect.width, rect.y0 + .38 * rect.height,
                         rect.x1 - .20 * rect.width, rect.y1 - .38 * rect.height))
            if first_line is None:
                first_line = line_index
            if line_index == first_line:
                replacement_box |= rect
        if first_line is not None:
            replacements[tuple(replacement_box)] = replacement
    if regions:
        for box in regions:
            page.add_redact_annot(pymupdf.Rect(box), fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        # Redaction drops fonts and graphics state used only by the detached content.
        for (group, name), original in resources.items() if resources else ():
            pdf = page.parent
            key = _resource_key(page, group, name)
            value = pdf.xref_get_key(*key)
            if value[0] == 'null':
                pdf.xref_set_key(*key, original[1])
            elif value != original:
                raise RuntimeError('A printed PDF resource conflicts with the searchable layer.')
        _search_layer(page, ((Line(value, box, ()), value)
                             for box, value in replacements.items()), font)
    return len(replacements)


def _checks_on_pages(original, pages):
    if not pages or len(pages) != len(original):
        raise ValueError('The positioned transcript must include every source page.')
    for index, description in enumerate(pages):
        page = original[index]
        if description.number != index + 1 or abs(page.rect.width - description.width) > .05 \
                or abs(page.rect.height - description.height) > .05:
            raise ValueError('A positioned transcript page does not match the source geometry.')
    if original.needs_pass:
        raise ValueError('The source PDF must be unlocked before processing.')


def _check_amount_pixels(image, lines, masks, sx, sy):
    """Retain a copy of any amount box that geometrically touches an edit."""
    edits = [pymupdf.Rect(mask.box) for mask in masks]
    nearby = []
    verified = 0
    for line in lines:
        for match in BILLING.finditer(line.text):
            amounts, fallback = _line_masks(line, [Edit(*match.span(), 'amount', match.group())])
            if fallback:
                continue
            for amount in amounts:
                box = pymupdf.Rect(amount.box)
                verified += 1
                if any(not (box & edit).is_empty for edit in edits):
                    pixels = _pixel_box(amount.box, sx, sy, image.width, image.height)
                    nearby.append((pixels, image.crop(pixels)))
    return verified, nearby


def _render_page(job):
    source, description, line_edits, font, dpi, barcodes = job
    totals = Counter()
    with pymupdf.open(source) as original:
        page = original[description.number - 1]
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=False)
        image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    sx, sy = image.width / description.width, image.height / description.height
    masks = []
    for i, edits in line_edits.items():
        line_masks, used_fallback = _line_masks(description.lines[i], edits)
        masks.extend(line_masks)
        totals['whole_line_masks'] += used_fallback
    if barcodes:
        codes = list(_barcode_masks(image, sx, sy))
        masks.extend(codes)
        totals['barcode_masks'] += len(codes)
    checked, nearby = _check_amount_pixels(image, description.lines, masks, sx, sy)
    totals['tight_typesetting_regions'] += PagePainter(font).stamp(image, masks, sx, sy)
    totals['amount_regions_examined'] += checked
    for box, before in nearby:
        if ImageChops.difference(before, image.crop(box)).getbbox() is not None:
            image.paste(before, box[:2])
            totals['amount_regions_restored'] += 1
    totals['amount_regions_with_changed_pixels'] += sum(
        ImageChops.difference(before, image.crop(box)).getbbox() is not None
        for box, before in nearby)
    if barcodes and codes:
        totals['remaining_barcodes'] += sum(1 for _ in _barcode_masks(image, sx, sy))
    totals['visual_masks'] += len(masks)
    totals['pages'] += 1
    buffer = io.BytesIO()
    r, g, b = image.split()
    if not (ImageChops.difference(r, g).getbbox() or ImageChops.difference(g, b).getbbox()):
        image = r
        totals['lossless_grayscale_pages'] += 1
    image.save(buffer, format='PNG', compress_level=2)
    return buffer.getvalue(), totals


def render(source, target, pages, plan, *, font_path, dpi=300, barcodes=False, workers=1,
           search_aliases=(), search_patients=()):
    font = pymupdf.Font(fontfile=str(font_path))
    amounts = Counter()
    edits_by_page = {}
    for (number, index), edits in plan.items():
        edits_by_page.setdefault(number, {})[index] = edits
    jobs = ((source, page, edits_by_page.get(page.number, {}), font_path, dpi, barcodes)
            for page in pages)
    with pymupdf.open(source) as original, pymupdf.open() as pdf, ExitStack() as stack:
        _checks_on_pages(original, pages)
        mapper = map
        if workers > 1:
            mapper = stack.enter_context(ProcessPoolExecutor(max_workers=workers)).map
        for description, (image, receipt) in zip(pages, mapper(_render_page, jobs), strict=True):
            amounts.update(receipt)
            texts = ((line, apply_edits(line.text, plan.get((description.number, i), [])))
                     for i, line in enumerate(description.lines))
            output = pdf.new_page(width=description.width, height=description.height)
            output.insert_image(output.rect, stream=image)
            _search_layer(output, texts, font)
            removed, remaining = _scrub_search_aliases(output, search_aliases, font, search_patients)
            amounts['search_layer_alias_fixes'] += removed
            amounts['search_layer_remaining_aliases'] += remaining
        pdf.set_metadata({'title': 'Synthetic Medical and Billing Records'})
        pdf.save(target, garbage=4, deflate=True, use_objstms=1)
    return dict(amounts)


def verify(source, target, pages, plan):
    totals = Counter()
    with pymupdf.open(source) as original, pymupdf.open(target) as synthetic:
        _checks_on_pages(original, pages)
        _checks_on_pages(synthetic, pages)
        if synthetic.embfile_count() or synthetic.get_toc() or synthetic.get_xml_metadata():
            raise RuntimeError('Unexpected attachments, outline, or XML metadata in the synthetic PDF.')
        for index, description in enumerate(pages):
            page = synthetic[index]
            if page.first_annot or page.first_widget or page.first_link or len(page.get_images()) != 1:
                raise RuntimeError('Unexpected interactive content or image structure in the synthetic PDF.')
            expected = Counter()
            for i, line in enumerate(description.lines):
                value = apply_edits(line.text, plan.get((description.number, i), []))
                expected.update(_amount_key(amount) for amount in BILLING.findall(value))
            actual = Counter(_amount_key(amount) for amount in BILLING.findall(page.get_text()))
            totals['searchable_amounts_checked'] += sum(expected.values())
            difference = sum((expected - actual).values())
            totals['searchable_amount_differences'] += difference
            totals['pages_with_amount_differences'] += bool(difference)
            totals['additional_searchable_amount_matches'] += sum((actual - expected).values())
    return dict(totals)


def _amount_key(value):
    # Column grouping can attach a separately positioned currency symbol to a figure.
    return re.sub(r'[\s$,]', '', value)


def _private_parent(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.stat()
    if path.parent.is_symlink() or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError('Use an owner-only output directory.')


def _key(path):
    if path is None:
        return secrets.token_bytes(32)
    # The key is a private case file, never a PDF field or a command-line argument.
    _private_parent(path)
    try:
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
            stream.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        key = stream.read(33)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or len(key) != 32:
            raise ValueError('The case key must be a private 32-byte file.')
        return key


def main():
    from faker import Faker

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--transcript', required=True, type=Path)
    parser.add_argument('--independent-source', type=Path, help='Optional second positioned OCR transcript')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--date-shift-days', required=True, type=int)
    parser.add_argument('--font', required=True, type=Path, help='Local TrueType font')
    parser.add_argument('--ner-model', help='Installed local spaCy model or filesystem path')
    parser.add_argument('--candidates', action='append', type=Path, default=[])
    parser.add_argument('--aliases', type=Path, help='Private JSON array of role and values records')
    parser.add_argument('--local-repairs', type=Path, help='Confirmed source-position fixes from local review')
    parser.add_argument('--key-file', type=Path, help='Optional private file to reproduce replacements')
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--barcodes', action='store_true', help='Scan locally and mask decoded barcodes')
    parser.add_argument('--review-draft', action='store_true',
                        help='Keep a private review PDF if searchable amounts or barcodes need review')
    args = parser.parse_args()
    if args.date_shift_days == 0 or not 150 <= args.dpi <= 600 or not 1 <= args.workers <= 16:
        parser.error('Provide a nonzero shift, 150 to 600 DPI, and 1 to 16 workers.')
    os.umask(0o077)
    _private_parent(args.output)
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError('Use a new synthetic PDF path.')
    before = input_digest(args.input)
    pages = list(read_transcript(args.transcript))
    pages, manual_dates, manual_candidates = load_repairs(args.local_repairs, pages, args.date_shift_days)
    corrections = []
    if args.independent_source:
        corrections = list(reconcile_dates(pages, list(read_transcript(args.independent_source)),
                                           args.date_shift_days))
    nlp = None
    if args.ner_model:
        import spacy

        nlp = spacy.load(args.ner_model, enable=['tok2vec', 'ner'])
    spellings = json.loads(args.aliases.read_text(encoding='utf-8')) if args.aliases else []
    plan, registry, receipt = make_plan(pages, days=args.date_shift_days, key=_key(args.key_file),
                                        faker=Faker('en_US'), nlp=nlp,
                                        extras=[*read_model_candidates(args.candidates), *manual_candidates],
                                        spellings=spellings, date_corrections=[*corrections, *manual_dates])
    print(json.dumps({'planned': receipt}), flush=True)
    with NamedTemporaryFile(dir=args.output.parent, suffix='.pdf') as output:
        rendered = render(args.input, output.name, pages, plan, font_path=args.font,
                          dpi=args.dpi, barcodes=args.barcodes, workers=args.workers,
                          search_aliases=_search_aliases(spellings, registry),
                          search_patients=patient_names(spellings))
        checks = verify(args.input, output.name, pages, plan)
        if before != input_digest(args.input):
            raise RuntimeError('Source PDF changed during processing.')
        findings = (checks.get('searchable_amount_differences') or rendered.get('remaining_barcodes')
                    or rendered.get('search_layer_remaining_aliases'))
        if findings and not args.review_draft:
            print(json.dumps({'rendered': rendered, 'verification': checks}), flush=True)
            raise RuntimeError('The PDF has billing or identifier findings that need review.')
        with open(output.name, 'rb') as result:
            os.fsync(result.fileno())
        os.link(output.name, args.output)
    print(json.dumps({'rendered': rendered, 'verification': checks,
                      'source_unchanged': True}), flush=True)


if __name__ == '__main__':
    main()
