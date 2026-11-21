"""Independently re-OCR a medical PDF and report aggregate verification results."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess

import pymupdf

from medical_anonymize import (
    BILLING, CONTACT, ID_FIELD, PEOPLE, SSN, alias_expression, apply_edits, canonical,
    input_digest, make_plan, patient_surname, read_model_candidates, read_transcript,
)
from medical_dates import calendar_spans, shift_dates
from patient_names import canonical_patient_group, patient_names, positioned_patient_spans
from pdf_ocr import Line, Page, Word, write_pages


def _tesseract(job):
    path, first, count, dpi, psm = job
    with pymupdf.open(path) as pdf:
        for index in range(first, count):
            source = pdf[index]
            pix = source.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
            sx, sy = source.rect.width / pix.width, source.rect.height / pix.height
            output = subprocess.run(
                ['tesseract', 'stdin', 'stdout', '--psm', str(psm), '-l', 'eng', 'tsv'],
                input=pix.tobytes('png'), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=True, timeout=120,
                env={**os.environ, 'OMP_THREAD_LIMIT': '1'},
            ).stdout
            grouped = defaultdict(list)
            for row in csv.DictReader(io.StringIO(output.decode('utf-8', 'replace')), delimiter='\t',
                                      quoting=csv.QUOTE_NONE):
                text = row.get('text', '').strip()
                if row.get('level') != '5' or not text:
                    continue
                group = (row['block_num'], row['par_num'], row['line_num'])
                left, top, width, height = map(int, (row['left'], row['top'], row['width'], row['height']))
                word = Word(text, (left * sx, top * sy, (left + width) * sx, (top + height) * sy))
                grouped[group].append((word, float(row['conf']) / 100))
            lines = []
            for values in grouped.values():
                words = tuple(word for word, _ in values)
                box = pymupdf.Rect(words[0].box)
                for word in words[1:]:
                    box |= pymupdf.Rect(word.box)
                lines.append(Line(' '.join(word.text for word in words), tuple(box), words,
                                  sum(score for _, score in values) / len(values)))
            yield Page(index + 1, source.rect.width, source.rect.height, 'ocr', tuple(lines))


def _job(job):
    return list(_tesseract(job))


def ocr_passes(path, profiles, workers):
    """Return one positioned transcript per (DPI, segmentation mode) profile."""
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError('The PDF must be unlocked before OCR.')
        count = len(pdf)
    chunks = range(0, count, 3)
    jobs = ((path, i, min(i + 3, count), dpi, psm) for dpi, psm in profiles for i in chunks)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = iter(executor.map(_job, jobs))
        for _ in profiles:
            yield [page for _ in chunks for page in next(results)]


def _sensitive_spelling(value, role):
    norm = canonical(value, role)
    if role in PEOPLE and len(norm.split()) < 2:
        return None
    if role == 'identifier' and len(norm) < 8:
        return None
    if len(norm) < 5:
        return None
    words = re.findall(r'[^\W_]+', value.casefold())
    return r'[\W_]*'.join(map(re.escape, words)) if words else None


def _compile(expressions):
    expressions = sorted(expressions, key=len, reverse=True)
    return [re.compile(r'(?<!\w)(?:' + '|'.join(expressions[i:i + 150]) + r')(?!\w)', re.I)
            for i in range(0, len(expressions), 150)]


def _known_patient_matches(text, patterns):
    hits = [match for pattern in patterns for match in pattern.finditer(text)]
    if hits:
        dates = calendar_spans(text)
        yield from (hit for hit in hits if not any(a <= hit.start() and hit.end() <= b for a, b in dates))


def _inline_fields(text):
    for regex, role in ((CONTACT, 'contact'), (SSN, 'identifier'), (ID_FIELD, 'identifier')):
        for found in regex.finditer(text):
            start, end = found.span('value') if regex is ID_FIELD else found.span()
            value = text[start:end]
            if regex is ID_FIELD and not any(c.isdigit() for c in value):
                continue
            yield start, end, role, canonical(value, role)


def compare(original, independent, visible, plan, *, days, spellings=()):
    """Compare a second OCR pass to the source and planned edits, without exposing values."""
    source_by_page = {page.number: page for page in original}
    reference_by_page = {page.number: page for page in independent}
    output_by_page = {page.number: page for page in visible}
    if set(source_by_page) != set(output_by_page) or set(source_by_page) != set(reference_by_page):
        raise ValueError('Both verification transcripts must cover the source page set.')

    patient_expressions = {
        expression
        for group in spellings if group['role'] == 'patient' and 'identity' not in group
        for value in [*group['values'], patient_surname(group)] if value
        if (expression := alias_expression(value))
    }
    known_patient = _compile(patient_expressions)
    identities = patient_names(spellings)
    expressions = defaultdict(set)
    allowed = set()
    for (page, i), edits in plan.items():
        text = source_by_page[page].lines[i].text
        for item in edits:
            if item.role != 'date':
                original_value = _sensitive_spelling(text[item.start:item.end], item.role)
                safe_value = _sensitive_spelling(item.replacement, item.role)
                if original_value:
                    expressions[item.role].add(original_value)
                if safe_value:
                    allowed.add(safe_value)
    rules = {role: _compile(items - allowed) for role, items in expressions.items()}
    receipt = Counter()
    receipt['known_patient_aliases_checked'] = len(patient_expressions) + sum(len(name.source_terms) for name in identities)
    receipt['known_patient_alias_source_pages'] = 0
    receipt['known_patient_alias_matches'] = 0
    receipt['pages_with_known_patient_aliases'] = 0
    flags = []
    searchable_pages = 0
    for number, output in output_by_page.items():
        reference = reference_by_page[number]
        source = source_by_page[number]
        searchable_pages += bool(output.lines)
        remaining = Counter()
        output_amounts = Counter()
        expected_dates = set()
        original_dates = set()
        original_fields = set()
        receipt['known_patient_alias_source_pages'] += any(
            any(_known_patient_matches(line.text, known_patient)) or
            any(any(name.spans(line.text)) for name in identities)
            for page in (source, reference) for line in page.lines
        )
        for i, line in enumerate(source.lines):
            shifted, _ = shift_dates(apply_edits(line.text, plan.get((number, i), [])), 0)
            expected_dates.update(item.original for item in shifted)
            dates, _ = shift_dates(line.text, 0)
            original_dates.update(item.original for item in dates)
        for line in reference.lines:
            dates, _ = shift_dates(line.text, days)
            original_dates.update(date.original for date in dates)
            if (line.confidence or 0) >= .60:
                original_fields.update((role, value) for _, _, role, value in _inline_fields(line.text))
        for i, line in enumerate(output.lines):
            output_amounts.update(BILLING.findall(line.text))
            for found in _known_patient_matches(line.text, known_patient):
                remaining['known_patient_alias_matches'] += 1
                flags.append((number, i, found.start(), found.end(), 'patient'))
            dates, unresolved = shift_dates(line.text, days)
            receipt['unresolved_visible_date_expressions'] += unresolved
            for item in dates:
                if item.original in expected_dates:
                    receipt['visible_dates_matching_shifted_values'] += 1
                else:
                    flag = 'unshifted_date' if item.original in original_dates else 'unverified_date'
                    remaining[flag] += 1
                    flags.append((number, i, item.start, item.end, flag))
            for role, patterns in rules.items():
                for pattern in patterns:
                    for found in pattern.finditer(line.text):
                        remaining[f'original_{role}'] += 1
                        flags.append((number, i, found.start(), found.end(), role))
            for start, end, role, value in _inline_fields(line.text):
                receipt['visible_contact_and_id_expressions'] += 1
                if (role, value) in original_fields:
                    remaining[f'unchanged_labeled_{role}'] += 1
                    flags.append((number, i, start, end, role))
        for name in identities:
            for group in positioned_patient_spans(output.lines, name, include_synthetic=True):
                if canonical_patient_group(output.lines, group, name):
                    continue
                if all(any(a <= item.span.start and item.span.end <= b
                           for a, b in calendar_spans(output.lines[item.line].text)) for item in group):
                    continue
                remaining['known_patient_alias_matches'] += 1
                flags.extend((number, item.line, item.span.start, item.span.end, 'patient') for item in group)
        reference_amounts = Counter(value for line in reference.lines for value in BILLING.findall(line.text))
        receipt['independent_amounts_seen_on_source'] += sum(reference_amounts.values())
        receipt['independent_amounts_re_read'] += sum((reference_amounts & output_amounts).values())
        receipt.update(remaining)
        receipt['pages_with_known_patient_aliases'] += bool(remaining['known_patient_alias_matches'])
        receipt['pages_with_visible_review_flags'] += bool(remaining)
    receipt['pages'] = len(output_by_page)
    receipt['readable_pages'] = searchable_pages
    return dict(receipt), flags


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    scan = subparsers.add_parser('scan')
    scan.add_argument('input', type=Path)
    scan.add_argument('--output', type=Path, required=True)
    scan.add_argument('--dpi', type=int, default=280)
    scan.add_argument('--psm', type=int, choices=(3, 6, 11), default=3)
    scan.add_argument('--workers', type=int, default=8)
    check = subparsers.add_parser('compare')
    check.add_argument('--transcript', type=Path, required=True)
    check.add_argument('--independent-source', type=Path, required=True)
    check.add_argument('--independent-output', type=Path, required=True)
    check.add_argument('--date-shift-days', type=int, required=True)
    check.add_argument('--key-file', type=Path, required=True)
    check.add_argument('--ner-model')
    check.add_argument('--candidates', type=Path, action='append', default=[])
    check.add_argument('--aliases', type=Path)
    check.add_argument('--local-repairs', type=Path)
    check.add_argument('--private-flags', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'scan':
        if not (1 <= args.workers <= 32 and 150 <= args.dpi <= 600):
            parser.error('Use 1 to 32 workers and 150 to 600 DPI.')
        before = input_digest(args.input)
        pages, = ocr_passes(args.input, [(args.dpi, args.psm)], args.workers)
        if before != input_digest(args.input):
            raise RuntimeError('PDF changed during independent OCR.')
        print(json.dumps(write_pages(args.output, pages)), flush=True)
        return

    from faker import Faker
    from pdf_synthetic import _key, _private_parent, load_repairs, reconcile_dates

    source = list(read_transcript(args.transcript))
    independent = list(read_transcript(args.independent_source))
    visible = list(read_transcript(args.independent_output))
    source, dates, candidates = load_repairs(args.local_repairs, source, args.date_shift_days)
    nlp = None
    if args.ner_model:
        import spacy

        nlp = spacy.load(args.ner_model, enable=['tok2vec', 'ner'])
    aliases = json.loads(args.aliases.read_text(encoding='utf-8')) if args.aliases else []
    unconfirmed = []
    plan, _, _ = make_plan(source, days=args.date_shift_days, key=_key(args.key_file),
                           faker=Faker('en_US'), nlp=nlp,
                           extras=[*read_model_candidates(args.candidates), *candidates], spellings=aliases,
                           date_corrections=[*reconcile_dates(source, independent, args.date_shift_days), *dates],
                           review_flags=unconfirmed)
    counts, flags = compare(source, independent, visible, plan, days=args.date_shift_days, spellings=aliases)
    flags.extend(unconfirmed)
    counts['unconfirmed_person_suggestions'] = len(unconfirmed)
    if args.private_flags:
        _private_parent(args.private_flags)
        with args.private_flags.open('x', encoding='utf-8') as stream:
            json.dump(flags, stream)
    print(json.dumps(counts), flush=True)
    if counts['known_patient_alias_matches']:
        if patient_names(aliases):
            raise SystemExit('An original or noncanonical patient name remains in the rendered pages.')
        raise SystemExit('Confirmed patient name or surname remains in the rendered pages.')
    if counts['known_patient_aliases_checked'] and not counts['known_patient_alias_source_pages']:
        raise SystemExit('The confirmed patient aliases did not match either source OCR transcript.')
    if unconfirmed:
        raise SystemExit('Unconfirmed person suggestions need review at their source positions.')


if __name__ == '__main__':
    main()
