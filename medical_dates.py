"""Shift complete calendar dates without interpreting durations or billing codes."""
from calendar import month_abbr, month_name
from dataclasses import dataclass
from datetime import date, timedelta
import re


_MONTHS = {name.lower(): i for names in (month_name, month_abbr)
           for i, name in enumerate(names) if name}
_MONTHS['sept'] = 9
_MONTH = '|'.join(sorted(_MONTHS, key=len, reverse=True))
_ORDINAL = r'(?:st|nd|rd|th)?'
_YEAR = r'(?:\d{4}|\d{2})'
_DATE = re.compile(
    rf'(?<![\w/.])(?:(?P<iso>(?P<iy>\d{{4}})\s*(?P<isep>[/.\-])\s*(?P<im>\d{{1,2}})'
    rf'\s*(?P=isep)\s*(?P<id>\d{{1,2}}))'
    rf'|(?P<us>(?P<um>\d{{1,2}})\s*(?P<usep>[/.\-])\s*(?P<ud>\d{{1,2}})'
    rf'\s*(?P=usep)\s*(?P<uy>{_YEAR}))'
    rf'|(?P<words>(?P<wm>{_MONTH})\.?[ \t-]+(?P<wd>\d{{1,2}}){_ORDINAL}[ ,\t-]+'
    rf'(?P<wy>\d{{4}}|\d{{2}}(?=\s*(?:$|[.;,)]))))'
    rf'|(?P<inverse>(?P<vd>\d{{1,2}}){_ORDINAL}[ \t-]+(?P<vm>{_MONTH})\.?[ ,\t-]+'
    rf'(?P<vy>{_YEAR})))(?=$|[^\w./]|[Tt]\d|\.(?!\d))', re.I)
_RANGE_START = re.compile(r'(?<![\w/.])(\d{1,2})\s*/\s*(\d{1,2})\s*(?:[-–—]|to|through)\s*$', re.I)
_PARTIAL = re.compile(
    rf'\b(?:{_MONTH})\.?\s+(?:19|20)\d{{2}}\b|'
    r'\b(?:DOB|Date of Birth|Date of Service|Service Date|Visit Date|Admit Date|'
    r'Discharge Date|Injury Date)\s*[:#]?\s*(?:\d{1,2}[/.-]\d{1,4}|(?:19|20)\d{2})'
    r'(?![\d/.-])', re.I)
_COMPACT = re.compile(
    r'\b(?:DOB|Date of Birth|Date of Service|Service Date|Visit Date|Admit Date|'
    r'Discharge Date|Injury Date)\s*[:#]?\s*(?P<value>\d{8})(?!\d)', re.I)


@dataclass(frozen=True)
class DateEdit:
    start: int
    end: int
    original: date
    shifted: date
    replacement: str


def _year(text):
    value = int(text)
    if len(text) == 4:
        return value
    return value + (1900 if value >= 69 else 2000)


def _ordinal(day):
    if 11 <= day % 100 <= 13:
        return 'th'
    return {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')


def _format_month(text, month):
    if text.lower() in _MONTHS and len(text) > 3 and text.lower() != 'sept':
        value = month_name[month]
    else:
        value = 'Sept' if text.lower() == 'sept' and month == 9 else month_abbr[month]
    if text.isupper():
        return value.upper()
    if text.islower():
        return value.lower()
    return value


def _format_part(value, raw):
    return str(value).zfill(len(raw))


def _full_edit(match, days):
    prefix = next(letter for letter in 'iuwv' if match[letter + 'y'] is not None)
    year, month, day = (prefix + part for part in 'ymd')
    raw_month = match[month]
    textual = prefix in 'wv'
    original = date(_year(match[year]), _MONTHS[raw_month.lower()] if textual else int(raw_month),
                    int(match[day]))
    shifted = original + timedelta(days=days)
    values = {
        year: f'{shifted.year % 100:02}' if len(match[year]) == 2 else f'{shifted.year:04}',
        month: _format_month(raw_month, shifted.month) if textual else
        _format_part(shifted.month, raw_month),
        day: str(shifted.day) if textual and not match[day].startswith('0') else
        _format_part(shifted.day, match[day]),
    }
    start, end = match.span()
    replacement = match.group()
    for group in sorted(values, key=match.start, reverse=True):
        left, right = match.span(group)
        value = values[group]
        ordinal = re.match(r'(st|nd|rd|th)\b', match.string[right:end], re.I) if group == day else None
        if ordinal:
            suffix = _ordinal(shifted.day)
            value += suffix.upper() if ordinal.group().isupper() else suffix
            right += len(ordinal.group())
        replacement = replacement[:left - start] + value + replacement[right - start:]
    return DateEdit(start, end, original, shifted, replacement)


def shift_dates(text, days):
    """Return exact replacement spans and the count of ambiguous calendar expressions."""
    edits = []
    unresolved = 0
    for match in _DATE.finditer(text):
        try:
            edit = _full_edit(match, days)
        except (ValueError, OverflowError):
            unresolved += 1
            continue
        # For M/D–M/D/Y, infer the first date's year from the stated end date.
        inferred = _RANGE_START.search(text[:edit.start]) if match['us'] else None
        if inferred and not any(prior.end > inferred.start() for prior in edits):
            month, day = map(int, inferred.groups())
            year = edit.original.year - ((month, day) > (edit.original.month, edit.original.day))
            try:
                original = date(year, month, day)
                shifted = original + timedelta(days=days)
                begin, end = inferred.start(), inferred.end(2)
                middle = text[inferred.end(1):inferred.start(2)]
                year_suffix = f'/{shifted.year:04}' if shifted.year != edit.shifted.year else ''
                replacement = (_format_part(shifted.month, inferred[1]) + middle +
                               _format_part(shifted.day, inferred[2]) + year_suffix)
                edits.append(DateEdit(begin, end, original, shifted, replacement))
            except (ValueError, OverflowError):
                unresolved += 1
        edits.append(edit)
    for match in _COMPACT.finditer(text):
        a, b = match.span('value')
        if any(edit.start < b and a < edit.end for edit in edits):
            continue
        value = match['value']
        try:
            if value[:2] in ('19', '20'):
                original = date(int(value[:4]), int(value[4:6]), int(value[6:]))
                shifted = original + timedelta(days=days)
                replacement = f'{shifted.year:04}{shifted.month:02}{shifted.day:02}'
            else:
                original = date(int(value[4:]), int(value[:2]), int(value[2:4]))
                if original.year < 1700:
                    raise ValueError('Implausible four-digit calendar year.')
                shifted = original + timedelta(days=days)
                replacement = f'{shifted.month:02}{shifted.day:02}{shifted.year:04}'
            edits.append(DateEdit(a, b, original, shifted, replacement))
        except (ValueError, OverflowError):
            unresolved += 1
    unresolved += sum(not any(edit.start < match.end() and match.start() < edit.end for edit in edits)
                      for match in _PARTIAL.finditer(text))
    return sorted(edits, key=lambda edit: edit.start), unresolved


def calendar_spans(text):
    dates, _ = shift_dates(text, 0)
    return [(date.start, date.end) for date in dates] + [match.span() for match in _PARTIAL.finditer(text)]
