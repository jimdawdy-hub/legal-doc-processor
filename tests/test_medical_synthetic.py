from datetime import timedelta
from itertools import permutations
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from faker import Faker
from PIL import Image, ImageChops
import pymupdf
import pytest

from medical_anonymize import apply_edits, make_plan
from medical_dates import shift_dates
from patient_names import patient_names
from pdf_name_repair import NameReplacement, _join_hits, _name_edits, _proof_from_source, load_rules, repair
from pdf_ocr import Line, Page, Word, read_pages
from pdf_review import compare, ocr_passes
from pdf_synthetic import _scrub_search_aliases, _search_aliases, load_repairs, reconcile_dates, render, verify


def text_page(*lines, number=1):
    return Page(number, 300, 400, 'native',
                tuple(Line(text, (10, 10 + 10 * i, 290, 19 + 10 * i), ())
                      for i, text in enumerate(lines)))


def plan_for(pages, **kwargs):
    return make_plan(pages, days=66, key=b'x' * 32, faker=Faker('en_US'), **kwargs)


def test_calendar_shift_preserves_intervals_amounts_and_durations():
    text = ('12/29/2023 and 2024-02-29T08:49, 01/31–02/01/24, December 31st, 2023. '
            'Return in 3 weeks or 6 months. Paid $188.50. DOB: February 2024.')
    dates, unresolved = shift_dates(text, 66)
    assert [(item.replacement, item.shifted - item.original) for item in dates] == [
        ('03/04/2024', timedelta(days=66)),
        ('2024-05-05', timedelta(days=66)),
        ('04/06', timedelta(days=66)),
        ('04/07/24', timedelta(days=66)),
        ('March 6th, 2024', timedelta(days=66)),
    ]
    assert dates[1].shifted - dates[0].shifted == dates[1].original - dates[0].original
    assert unresolved == 1
    plan, _, receipt = plan_for([text_page(text)])
    result = apply_edits(text, plan[1, 0])
    assert 'T08:49' in result and '3 weeks or 6 months. Paid $188.50' in result
    assert receipt['date_replacements'] == 5


def test_identity_variants_and_second_pass_share_one_role_scoped_mapping():
    page = text_page(
        'Patient Name: Rowan Jexley',
        'Patient Name: JEXLEY, ROWAN',
        'Patient Name: Rowan Jexiey',
        'Emergency Contact: Maren Jexley',
        'Provider: Dr. Emmet Tenley, MD',
        'Dr. Tenley advised 3 weeks.',
        'ROWWAN JEXLEE was referred here.',
        'Facility: Zephyr Plains Clinic',
        'Insurance: Zephyr Plains Health Plan',
        'Previously Zephyr Plains Clinic.',
    )
    spellings = [{'role': 'patient', 'values': ['Rowan Jexley', 'ROWWAN JEXLEE']}]
    plan, _, _ = plan_for([page], spellings=spellings)
    person = [plan[1, i][0].replacement for i in range(10)]
    assert len({person[i] for i in (0, 1, 2, 6)}) == 1
    assert person[3] != person[0]
    assert person[4] == person[5] and person[4] != person[0]
    assert person[7] == person[9] and person[7] != person[8]
    assert apply_edits(page.lines[4].text, plan[1, 4]).endswith(', MD')
    assert 'advised 3 weeks.' in apply_edits(page.lines[5].text, plan[1, 5])


def test_confirmed_patient_surname_uses_same_pseudonym_without_a_name_label():
    page = text_page('Patient: Rowan Jexley', 'JEXLEY', 'Provider: Dr. Mira Jexley, MD', 'Total: $95.49')
    aliases = [{'role': 'patient', 'values': ['Rowan Jexley']}]
    plan, _, _ = plan_for([page], spellings=aliases)
    patient = plan[1, 0][0].replacement
    assert apply_edits(page.lines[1].text, plan[1, 1]) == patient.rsplit(' ', 1)[-1]
    assert plan[1, 2][0].role == 'physician'
    assert plan[1, 2][0].replacement != patient
    assert (1, 3) not in plan


STRUCTURED_PATIENT = {
    'role': 'patient',
    'identity': {'given': 'Avery', 'middle': 'Quinn', 'surname': 'Vale', 'suffix': 'Jr.'},
    'replacement': 'Morgan Finch',
}


def test_structured_patient_name_replaces_every_nonempty_subset_and_order():
    parts = ('AVERY', 'QUINN', 'VALE', 'JR.')
    samples = [', '.join(order) for length in range(1, 5) for order in permutations(parts, length)]
    pages = [text_page(f'Patient: {value}', number=i + 1) for i, value in enumerate(samples)]
    plan, _, _ = plan_for(pages, spellings=[STRUCTURED_PATIENT])
    for page in pages:
        assert apply_edits(page.lines[0].text, plan[page.number, 0]) == 'Patient: Morgan Finch'


def test_initials_split_fields_and_unrelated_suffixes():
    page = text_page('Patient: VALE', 'JR., AVERY Q.', 'Provider: Dr. Casey Reed Jr.', 'Total: $520.67')
    plan, _, _ = plan_for([page], spellings=[STRUCTURED_PATIENT])
    output = [apply_edits(line.text, plan.get((1, i), [])) for i, line in enumerate(page.lines)]
    assert 'VALE' not in ' '.join(output) and 'AVERY' not in ' '.join(output)
    assert sum(value.count('Morgan Finch') for value in output) == 1
    assert output[2].endswith('Jr.')
    assert output[3] == 'Total: $520.67'


def test_confirmed_patient_name_with_ocr_split_letters_on_both_passes():
    page = text_page('Patient: V A L E JR., AV ERY QUI NN', 'A V E R Y returned.')
    plan, _, _ = plan_for([page], spellings=[STRUCTURED_PATIENT])
    assert [apply_edits(line.text, plan.get((1, i), [])) for i, line in enumerate(page.lines)] == [
        'Patient: Morgan Finch', 'Morgan Finch returned.',
    ]
    counts, _ = compare([page], [page], [page], plan, days=66, spellings=[STRUCTURED_PATIENT])
    assert counts['known_patient_alias_matches'] == 2


def test_patient_surname_shared_by_a_month_preserves_calendar_dates(font_path):
    page = text_page('Patient: Rowan May', 'MAY', 'Visit scheduled March 1, 2024.', 'Recorded May 2022.')
    aliases = [{'role': 'patient', 'values': ['Rowan May']}]
    plan, registry, _ = plan_for([page], spellings=aliases)
    result = [apply_edits(line.text, plan.get((1, i), [])) for i, line in enumerate(page.lines)]
    assert result[2:] == ['Visit scheduled May 6, 2024.', 'Recorded May 2022.']
    assert result[1] != 'MAY'
    counts, _ = compare([page], [page], [text_page(*result)], plan, days=66, spellings=aliases)
    assert counts['known_patient_alias_matches'] == 0
    with pymupdf.open() as pdf:
        out = pdf.new_page()
        out.insert_text((42, 42), 'Return to May 6, 2024.')
        out.insert_text((42, 68), 'Name: MAY', render_mode=3)
        font = pymupdf.Font(fontfile=font_path)
        assert _scrub_search_aliases(out, _search_aliases(aliases, registry), font) == (1, 0)
        assert 'May 6, 2024.' in out.get_text()


@pytest.fixture
def font_path():
    if not shutil.which('fc-match'):
        pytest.skip('A local TrueType font is required to render this test.')
    path = subprocess.check_output(['fc-match', '-f', '%{file}', 'Liberation Sans']).decode().strip()
    return path


def test_rebuilt_pdf_removes_source_pixels_and_hidden_data_but_keeps_bill(tmp_path, font_path):
    path = tmp_path / 'source.pdf'
    target = tmp_path / 'synthetic.pdf'
    with pymupdf.open() as pdf:
        native = pdf.new_page(width=300, height=220)
        native.insert_text((20, 44), 'Patient: Rowan Jexley')
        native.insert_text((20, 69), 'DOB: 03/01/2024')
        native.insert_text((20, 82), 'JEXLEY')
        native.insert_text((20, 110), 'Total: $581.45')
        native.draw_rect((20, 87, 185, 125), color=(0, 0, 0))
        native.draw_rect((27, 140, 37, 150), color=(0, 0, 0))
        native.draw_line((28, 147), (36, 142), color=(0, 0, 0))
        scan = native.get_pixmap(dpi=144)
        pdf.new_page(width=300, height=220).insert_image((0, 0, 300, 220), pixmap=scan)
        pdf[1].insert_text((20, 44), 'Stale Hidden Name', render_mode=3)
        pdf.embfile_add('original.txt', b'Stale Hidden Name')
        pdf.set_metadata({'author': 'Rowan Jexley'})
        pdf.save(path)

    native, = read_pages(path, None, numbers={1})

    def positioned(_pixmap):
        for line in native.lines:
            words = tuple(Word(word.text, tuple(point * 2 for point in word.box)) for word in line.words)
            yield Line(line.text, tuple(point * 2 for point in line.box), words)

    pages = list(read_pages(path, positioned, dpi=144))
    first_bytes = path.read_bytes()
    aliases = [{'role': 'patient', 'values': ['Rowan Jexley']}]
    plan, _, _ = plan_for(pages, spellings=aliases)
    rendering = render(path, target, pages, plan, font_path=font_path, dpi=144)
    assert verify(path, target, pages, plan)['searchable_amount_differences'] == 0
    assert rendering['amount_regions_examined'] == 2
    assert rendering['amount_regions_with_changed_pixels'] == 0
    assert path.read_bytes() == first_bytes

    with pymupdf.open(target) as pdf:
        assert len(pdf) == 2
        assert pdf.embfile_count() == 0 and pdf.metadata['author'] == ''
        for page in pdf:
            decoded = page.get_text()
            assert '05/06/2024' in decoded and '$581.45' in decoded
            assert 'Rowan Jexley' not in decoded and 'Stale Hidden Name' not in decoded
            assert 'JEXLEY' not in decoded
            assert '03/01/2024' not in decoded
            embedded = page.get_images()[0][0]
            edited = pymupdf.Pixmap(pdf, embedded)
            if edited.n == 1:
                edited = pymupdf.Pixmap(pymupdf.csRGB, edited)
            original_image = Image.frombytes('RGB', (scan.width, scan.height), scan.samples)
            final_image = Image.frombytes('RGB', (edited.width, edited.height), edited.samples)
            diff = ImageChops.difference(original_image, final_image)
            assert diff.getbbox() is not None
            assert diff.crop((35, 146, 165, 173)).getbbox() is not None
            assert diff.crop((0, 180, scan.width, scan.height)).getbbox() is None


def test_independent_review_flags_remnant_names_and_dates():
    source = text_page('Patient: Rowan Jexley', 'DOB: 03/01/2024', 'Total: $581.45')
    plan, _, _ = plan_for([source])
    replacement = plan[1, 0][0].replacement
    passed = text_page(f'Patient: {replacement}', 'DOB: 05/06/2024', 'Total: $581.45')
    clean, _ = compare([source], [source], [passed], plan, days=66)
    assert clean['pages_with_visible_review_flags'] == 0
    failed, flags = compare([source], [source], [source], plan, days=66)
    assert failed['unshifted_date'] == 1 and failed['original_patient'] >= 1
    assert {flag[-1] for flag in flags} == {'patient', 'unshifted_date'}
    assert failed['independent_amounts_re_read'] == 1


def test_independent_review_finds_a_confirmed_surname_even_if_plan_missed_it():
    src = text_page('Patient: Rowan Jexley', 'Returned with original orders.')
    visible = text_page('Patient: Lucille Avery', 'Later JEXLEY returned.')
    aliases = [{'role': 'patient', 'values': ['Rowan Jexley']}]
    counts, flags = compare([src], [src], [visible], {}, days=66, spellings=aliases)
    assert counts['known_patient_alias_source_pages'] == 1
    assert counts['known_patient_alias_matches'] == 1
    assert counts['pages_with_known_patient_aliases'] == 1
    assert flags == [(1, 1, 6, 12, 'patient')]
    clean, _ = compare([src], [src], [text_page('Patient: Lucille Avery')], {}, days=66, spellings=aliases)
    assert clean['known_patient_alias_matches'] == 0


def test_independent_check_catches_mixed_names_and_missing_parts():
    original = text_page('Patient: Avery Quinn Vale Jr.')
    mixed = text_page('Patient: Morgan Finch Quinn', 'Entry begins', 'Finch', 'JR., AVERY')
    aliases = [STRUCTURED_PATIENT]
    leak, flags = compare([original], [original], [mixed], {}, days=66, spellings=aliases)
    assert leak['known_patient_alias_matches'] == 2
    assert {row[1] for row in flags} == {0, 2, 3}
    repaired = text_page('Patient: Morgan Finch', 'Entry begins', 'Morgan Finch')
    clean, _ = compare([original], [original], [repaired], {}, days=66, spellings=aliases)
    assert clean['known_patient_alias_matches'] == 0


def test_review_and_generation_catch_repeated_synthetic_given_names():
    aliases = [STRUCTURED_PATIENT]
    original = text_page('Patient: Avery Quinn Vale Jr.')
    repeated = text_page('Patient: Morgan Morgan Finch', 'Patient: Morgan Finch Morgan',
                         'Separate entries: Morgan Finch Morgan Finch')
    counts, flags = compare([original], [original], [repeated], {}, days=66, spellings=aliases)
    assert counts['known_patient_alias_matches'] == 2
    assert {flag[1] for flag in flags} == {0, 1}
    plan, _, _ = plan_for([repeated], spellings=aliases)
    assert [apply_edits(line.text, plan.get((1, i), [])) for i, line in enumerate(repeated.lines)] == [
        'Patient: Morgan Finch', 'Patient: Morgan Finch', 'Separate entries: Morgan Finch Morgan Finch',
    ]
    clinicians = text_page('Provider: Dr. Casey Finch, MD', 'Provider: Dr. Morgan Finch, MD')
    plan, _, _ = plan_for([clinicians], spellings=aliases)
    assert [plan[1, i][0].role for i in range(2)] == ['physician', 'physician']


def test_canonical_correction_erases_extras_and_preserves_separate_full_names(tmp_path):
    config = tmp_path / 'name-fixes.json'
    config.write_text(json.dumps([{'canonical': 'Morgan Finch'}, {'from': 'Avery', 'to': 'Morgan'}]))
    rules = load_rules(config)
    for before, after in [
        ('MORGAN Morgan Finch', 'Morgan Finch'),
        ('Morgan Finch Jr. MORGAN', 'Morgan Finch Jr.'),
        ('Morgan Finch Morgan Jr.', 'Morgan Finch Jr.'),
        ('Morgan Finch, Morgan', 'Morgan Finch,'),
        ('Morgan Finch Morgan,', 'Morgan Finch'),
        ('Morgan, Finch Morgan', 'Morgan, Finch'),
        ('Morgan Finch Morgan Finch', 'Morgan Finch Morgan Finch'),
        ('Morgan Finch. Morgan was seen', 'Morgan Finch. Morgan was seen'),
        ('Patient: Avery Morgan Finch', 'Patient: Morgan Finch'),
        ('Patient: Morgan Avery Finch', 'Patient: Morgan Finch'),
        ('Patient: Morgan Finch Avery', 'Patient: Morgan Finch'),
        ('Patient: Avery Finch Morgan', 'Patient: Morgan Finch'),
    ]:
        assert apply_edits(before, _name_edits(before, rules)) == after


def test_canonical_correction_joins_only_words_on_the_same_printed_row(tmp_path):
    config = tmp_path / 'name-fixes.json'
    config.write_text(json.dumps([{'canonical': 'Morgan Finch'}]))
    rules = load_rules(config)
    name = Line('Morgan Finch', (20, 50, 90, 62),
                (Word('Morgan', (20, 50, 55, 62)), Word('Finch', (60, 50, 90, 62))))
    for extra_box, expected in [((95, 51, 135, 63), 1), ((95, 75, 135, 87), 0), ((160, 50, 200, 62), 0)]:
        extra = Line('Morgan', extra_box, (Word('Morgan', extra_box),))
        page = Page(1, 300, 400, 'ocr', (name, extra))
        edits = _join_hits([page], rules)
        assert len(edits) == expected
        assert all(mask.replacement == '' for mask in edits)
    left = Line('Label Morgan,', (10, 20, 55, 62),
                (Word('Label', (10, 20, 35, 32)), Word('Morgan,', (20, 50, 55, 62))))
    right = Line('Other Finch Morgan', (5, 50, 135, 90),
                 (Word('Other', (5, 78, 30, 90)), Word('Finch', (60, 50, 90, 62)),
                  Word('Morgan', (95, 51, 135, 63))))
    page = Page(1, 300, 400, 'ocr', (left, right))
    extra, = _join_hits([page], rules)
    assert extra.box[0] > 90 and extra.replacement == ''
    displaced_name = Line('Morgan,', (92, 46, 127, 56), (Word('Morgan,', (92, 46, 127, 56)),))
    overlapping_word = Line('small', (91, 55, 117, 62), (Word('small', (91, 55, 117, 62)),))
    page = Page(1, 300, 400, 'ocr', (name, overlapping_word, displaced_name))
    extra, = _join_hits([page], rules)
    assert extra.box[1] < 47 and extra.box[3] == 55 and not extra.replacement


def test_searchable_mixed_patient_name_keeps_adjacent_billing_text(font_path):
    font = pymupdf.Font(fontfile=font_path)
    patients = patient_names([STRUCTURED_PATIENT])
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((30, 44), 'Morgan Finch Quinn', render_mode=3)
        page.insert_text((30, 56), 'Charge $37.92', render_mode=3)
        page.insert_text((30, 68), 'Finch', render_mode=3)
        page.insert_text((30, 80), 'JR., AVERY', render_mode=3)
        assert _scrub_search_aliases(page, (), font, patients) == (2, 0)
        result = page.get_text()
        assert result.count('Morgan Finch') == 2
        assert 'QUINN' not in result.upper() and 'AVERY' not in result.upper() and 'JR.' not in result.upper()
        assert '$37.92' in result


def test_independent_date_correction_needs_matching_digits_and_position():
    def page(*values):
        return Page(1, 300, 300, 'ocr', tuple(
            Line(value, (15, top, 100, top + 10),
                 (Word(value, (15, top, 100, top + 10)),), .92)
            for top, value in values
        ))

    primary = page((10, '03/0l/2024'), (30, '12/31/2023'), (50, '$412.34'))
    alternate = page((10, '03/01/2024'), (30, '12/30/2023'), (50, '04/12/2034'))
    corrections = list(reconcile_dates([primary], [alternate], 66))
    assert len(corrections) == 1
    assert corrections[0][0] == (1, 0)
    plan, _, totals = plan_for([primary], date_corrections=corrections)
    assert apply_edits(primary.lines[0].text, plan[1, 0]) == '05/06/2024'
    assert apply_edits(primary.lines[1].text, plan[1, 1]) == '03/06/2024'
    assert (1, 2) not in plan
    assert totals['independent_date_corrections'] == 1


def test_local_repairs_add_missing_date_at_confirmed_geometry(tmp_path):
    repair = tmp_path / 'repair.json'
    repair.write_text('[[1, -1, 0, 10, "date", [14, 40, 72, 49], "12/29/2023"]]')
    pages, dates, extras = load_repairs(repair, [text_page('Total: $218.10')], 66)
    target = pages[0].lines[1]
    assert target.words[0].box == (14, 40, 72, 49)
    plan, _, receipt = plan_for(pages, date_corrections=dates, extras=extras)
    assert apply_edits(target.text, plan[1, 1]) == '03/04/2024'
    assert receipt['date_replacements'] == 1
    assert (1, 0) not in plan


def test_known_phone_number_is_replaced_in_all_observed_formats():
    page = text_page('Phone (202) 555-0148', 'Fax 2025550148', 'Reference 2025550148542')
    plan, _, _ = plan_for([page])
    assert plan[1, 0][0].replacement == plan[1, 1][0].replacement
    assert (1, 2) not in plan


def test_split_alias_scrub_changes_only_search_text(font_path):
    spelling = [{'role': 'patient', 'values': ['Rowan Jexley']}]
    _, registry, _ = plan_for([text_page('Rowan', 'Jexley')], spellings=spelling)
    font = pymupdf.Font(fontfile=font_path)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=260, height=180)
        page.draw_rect((10, 15, 220, 100), color=(0, 0, 0))
        original = page.get_pixmap().samples
        page.insert_text((30, 30), 'Rowan', render_mode=3)
        page.insert_text((30, 46), 'Jexley', render_mode=3)
        page.insert_text((30, 76), 'Amount $174.30', render_mode=3)
        page.insert_text((30, 88), 'Reference: JEXLEY', render_mode=3)
        fixed, remaining = _scrub_search_aliases(page, _search_aliases(spelling, registry), font)
        assert (fixed, remaining) == (2, 0)
        assert 'rowan' not in page.get_text().casefold() and 'jexley' not in page.get_text().casefold()
        assert '$174.30' in page.get_text()
        assert page.get_pixmap().samples == original


def test_search_text_redaction_preserves_overlapping_printed_text(font_path):
    font = pymupdf.Font(fontfile=font_path)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 60), 'Avery', render_mode=3)
        page.insert_text((40, 60), 'Signed', render_mode=0)
        pixels = page.get_pixmap().samples
        patients = patient_names([STRUCTURED_PATIENT])
        assert _scrub_search_aliases(page, (), font, patients) == (1, 0)
        assert 'Morgan Finch' in page.get_text()
        assert 'Signed' in page.get_text()
        assert page.get_pixmap().samples == pixels


def test_reference_proof_checks_each_reading_before_merging_its_box():
    def positioned(end, confidence):
        box = (30, 31, end, 52)
        return Page(1, 300, 400, 'ocr', (Line('AVERY', box, (Word('AVERY', box),), confidence),))

    with pymupdf.open() as original:
        page = original.new_page(width=300, height=400)
        page.insert_text((31, 48), 'AVERY', fontsize=12)
        page.draw_rect((70, 31, 88, 51), fill=(0, 0, 0))
        pix = page.get_pixmap(dpi=144)
        reviewed = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        reviewed.paste('white', (138, 58, 180, 106))
        rules = [NameReplacement('Avery', 'Morgan').rule()]
        matches = _proof_from_source(page, [positioned(89, .98), positioned(67, .85)],
                                     reviewed, [], rules, 2, 2)
    assert [match.replacement for match in matches] == ['MORGAN']


def test_reviewed_pdf_name_repair_uses_local_ocr_and_keeps_pdf_layout(tmp_path, font_path):
    if not shutil.which('tesseract'):
        pytest.skip('The repair command requires local Tesseract.')
    source, output = tmp_path / 'synthetic.pdf', tmp_path / 'corrected.pdf'
    rules = tmp_path / 'name-fixes.json'
    rules.write_text(json.dumps([{'from': 'Avery', 'to': 'Morgan'}]))
    with pymupdf.open() as scan, pymupdf.open() as pdf:
        original = scan.new_page(width=360, height=280)
        original.insert_text((30, 60), 'Patient: AVERY', fontsize=18)
        original.insert_text((30, 90), 'Prefiled-XaveryY', fontsize=18)
        original.insert_text((30, 146), 'Visit: 04/16/2022    Charge: $629.17', fontsize=14)
        original.draw_rect((29, 174, 41, 186), color=(0, 0, 0))
        original.draw_line((30, 178), (39, 183))
        page = pdf.new_page(width=360, height=280)
        page.insert_image(page.rect, pixmap=original.get_pixmap(dpi=180))
        page.insert_text((30, 60), 'Patient: AVERY', fontsize=18, render_mode=3)
        page.insert_text((30, 90), 'Prefiled-XaveryY', fontsize=18, render_mode=3)
        page.insert_text((30, 146), 'Visit: 04/16/2022    Charge: $629.17', fontsize=14, render_mode=3)
        page.insert_text((30, 115), 'Avery', fontsize=12, render_mode=3)
        page.insert_text((30, 115), 'Approved', fontsize=12)
        pdf.save(source, deflate=True)
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'pdf_name_repair.py'), str(source),
               '--font', font_path, '--workers', '2', '--replacements', str(rules), '--output', str(output)]
    original_bytes = source.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
    receipt = json.loads(result.stdout)
    assert receipt['visible_replacements'] == 2 and receipt['searchable_replacements'] == 3
    assert receipt['pages_checked'] == 1 and receipt['clean_ocr_passes'] == 3
    assert source.read_bytes() == original_bytes
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pymupdf.open(source) as before, pymupdf.open(output) as after:
        page = after[0]
        text = page.get_text()
        assert page.rect == before[0].rect
        assert all(value in text for value in ('MORGAN', 'morgan', 'Approved', '04/16/2022', '$629.17'))
        assert 'avery' not in text.casefold()
        protected = pymupdf.Rect(0, 100, 360, 280)
        assert page.get_pixmap(clip=protected).samples == before[0].get_pixmap(clip=protected).samples
    visible, = ocr_passes(output, ((360, 11),), 1)
    assert sum(line.text.casefold().count('morgan') for line in visible[0].lines) == 2
    original_result = output.read_bytes()
    duplicate = subprocess.run(command, capture_output=True, timeout=15)
    assert duplicate.returncode and output.read_bytes() == original_result


def test_repair_removes_duplicate_names_from_ocr_when_search_text_is_clean(tmp_path, font_path):
    if not shutil.which('tesseract'):
        pytest.skip('The repair command requires local Tesseract.')
    source, output = tmp_path / 'synthetic.pdf', tmp_path / 'corrected.pdf'
    config = tmp_path / 'name-fixes.json'
    config.write_text(json.dumps([{'canonical': 'Morgan Finch'}]))
    with pymupdf.open() as scan, pymupdf.open() as pdf:
        original = scan.new_page(width=360, height=240)
        original.insert_text((30, 60), 'Patient: MORGAN Morgan Finch', fontsize=16)
        original.insert_text((30, 92), 'Morgan Finch Morgan', fontsize=16)
        original.insert_text((30, 146), 'Visit: 04/16/2022    Charge: $629.17', fontsize=14)
        page = pdf.new_page(width=360, height=240)
        page.insert_image(page.rect, pixmap=original.get_pixmap(dpi=180))
        page.insert_text((30, 60), 'Patient: Morgan Finch', render_mode=3)
        page.insert_text((30, 92), 'Morgan Finch', render_mode=3)
        page.insert_text((30, 146), 'Visit: 04/16/2022    Charge: $629.17', render_mode=3)
        pdf.save(source, deflate=True)
    rules = load_rules(config)
    receipt = repair(source, output, rules, font_path=font_path, workers=2)
    assert receipt['visible_replacements'] == 2 and receipt['searchable_replacements'] == 0
    with pymupdf.open(source) as before, pymupdf.open(output) as after:
        page = after[0]
        assert page.get_text() == before[0].get_text()
        billing = pymupdf.Rect(0, 115, 360, 240)
        assert page.get_pixmap(clip=billing).samples == before[0].get_pixmap(clip=billing).samples
    visible, = ocr_passes(output, ((360, 11),), 1)
    names = ' '.join(line.text for line in visible[0].lines).casefold()
    assert names.count('morgan') == names.count('morgan finch') == 2
