"""Match the confirmed components of one patient name in any printed order."""
from dataclasses import dataclass
from functools import cached_property
import re


_NAME = re.compile(r"[^\W\d_]+(?:[ ,.'’\-]+[^\W\d_]+)*\.?")
_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
_JOIN = re.compile(r"[ \t,()'’\-]*")
_PATIENT_LABEL = re.compile(
    r"\b(?:patient(?:'?s)?(?:\s+(?:full\s+)?name)?|name\s+of\s+(?:the\s+)?patient|"
    r"(?:member|insured|subscriber|guarantor)\s+name)\s*[:=]\s*$", re.I,
)


def spaced_word(word):
    """Match an OCR word that may have spaces between its printed letters."""
    return r'[ \t]*'.join(map(re.escape, word)) if len(word) >= 4 else re.escape(word)


def _expression(text):
    return r"[ \t.,'’\-]*".join(spaced_word(part) for part in re.findall(r"[^\W_]+", text))


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value.strip()):
        raise ValueError('Invalid confirmed patient name component.')
    return ' '.join(value.split())


@dataclass(frozen=True)
class PatientSpan:
    start: int
    end: int
    components: frozenset[str]
    anchored: bool
    canonical: bool


@dataclass(frozen=True)
class PositionedPatientSpan:
    line: int
    span: PatientSpan
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class PatientName:
    given: str
    middle: tuple[str, ...]
    surname: str
    suffix: str | None
    aliases: tuple[str, ...]
    replacement: str

    @classmethod
    def from_record(cls, record):
        identity = record['identity']
        middle = identity.get('middle', [])
        if isinstance(middle, str):
            middle = [middle]
        if not isinstance(middle, (list, tuple)):
            raise ValueError('Patient middle names must be strings.')
        return cls(_name(identity['given']), tuple(_name(value) for value in middle),
                   _name(identity['surname']), _name(identity['suffix']) if identity.get('suffix') else None,
                   tuple(_name(value) for value in record.get('values', [])), _name(record['replacement']))

    @cached_property
    def source_terms(self):
        terms = [(self.given, 'given'), (self.surname, 'surname')]
        terms.extend((value, 'middle') for value in self.middle)
        terms.extend((value, 'alias') for value in self.aliases)
        return tuple(terms)

    def _pattern(self, *, include_synthetic):
        terms = {}
        for value, component in sorted(self.source_terms, key=lambda item: -len(item[0])):
            terms.setdefault(_expression(value), component)
        if include_synthetic:
            words = _WORD.findall(self.replacement)
            if words and words[-1].casefold() in {'jr', 'sr', 'junior', 'senior', 'ii', 'iii', 'iv'}:
                words.pop()
            for index, word in enumerate(words):
                component = 'synthetic_surname' if index == len(words) - 1 else 'synthetic_given'
                terms.setdefault(_expression(word), component)
        initials = {value[0].casefold() for value in (self.given, self.surname, *self.middle)}
        for initial in initials:
            terms.setdefault(re.escape(initial) + r'\.?', 'initial')
        if self.suffix:
            value = _expression(self.suffix)
            terms.setdefault(value + r'\.?', 'suffix')
            if self.suffix.casefold().rstrip('.') == 'jr':
                terms.setdefault(r'junior', 'suffix')
        ordered = list(terms)
        return (re.compile(r'(?<![^\W_])(?:' + '|'.join(f'(?P<n{i}>{x})'
                           for i, x in enumerate(ordered)) + r')(?![^\W_])', re.I),
                tuple(terms[x] for x in ordered))

    @cached_property
    def source_pattern(self):
        return self._pattern(include_synthetic=False)

    @cached_property
    def output_pattern(self):
        return self._pattern(include_synthetic=True)

    def spans(self, text, *, include_synthetic=False, fragments=False, across_lines=False):
        """Read maximal patient-name runs, allowing any subset or order of confirmed parts."""
        pattern, kinds = self.output_pattern if include_synthetic else self.source_pattern
        matches = []
        for match in pattern.finditer(text):
            kind = kinds[int(match.lastgroup[1:])]
            if kind == 'initial' and not (match[0][0].isupper() or match[0].endswith('.')):
                continue
            gap = text[matches[-1][1]:match.start()] if matches else ''
            if across_lines and gap.count('\n') == 1:
                gap = gap.replace('\n', ' ')
            next_complete_name = (matches and kind == 'synthetic_given'
                                  and self.is_canonical(text[matches[-1][0]:matches[-1][1]]))
            if matches and _JOIN.fullmatch(gap) and not next_complete_name:
                matches[-1][1] = match.end()
                matches[-1][2].add(kind)
            else:
                matches.append([match.start(), match.end(), {kind}])
        for start, end, components in matches:
            context = bool(_PATIENT_LABEL.search(text[:start]))
            anchored = context or bool(components & {'given', 'middle', 'surname', 'alias', 'synthetic_surname'})
            if not anchored and not fragments:
                continue
            if end < len(text) and text[end] == ')' and text[start:end].count('(') > text[start:end].count(')'):
                end += 1
            same = self.is_canonical(text[start:end])
            yield PatientSpan(start, end, frozenset(components), anchored, same)

    def is_canonical(self, value):
        return ' '.join(value.casefold().split()) == self.replacement.casefold()


def patient_names(spellings):
    return tuple(PatientName.from_record(record) for record in spellings
                 if record['role'] == 'patient' and 'identity' in record)


def _complete(components):
    return bool(components & {'alias'}) or (
        bool(components & {'given', 'synthetic_given'}) and
        bool(components & {'surname', 'synthetic_surname'})
    )


def _adjacent(lines, left, right, grouped_components):
    base_parts = {'given', 'middle', 'surname'}
    existing = {part.removeprefix('synthetic_') for part in grouped_components}
    following = {part.removeprefix('synthetic_') for part in right.span.components}
    if (left.line == right.line or existing & following & base_parts
            or (_complete(grouped_components) and _complete(right.span.components))):
        return False
    if not _JOIN.fullmatch(lines[left.line].text[left.span.end:]) or not _JOIN.fullmatch(
        lines[right.line].text[:right.span.start]
    ):
        return False
    x0, y0, x1, y1 = left.box
    a0, b0, _, b1 = right.box
    height = max(y1 - y0, b1 - b0)
    dy = ((b0 + b1) - (y0 + y1)) / 2
    if abs(dy) <= .55 * height:
        return a0 >= x0 and -2 <= a0 - x1 <= 18
    return .55 * height < dy <= 2.05 * height and -12 <= a0 - x0 <= 18


def positioned_patient_spans(lines, name, *, include_synthetic=False, span_box=None):
    """Combine name fragments printed in neighboring boxes of the same field."""
    positioned = []
    for i, line in enumerate(lines):
        for span in name.spans(line.text, include_synthetic=include_synthetic, fragments=True):
            box = span_box(line, span) if span_box else line.box
            if box is not None:
                positioned.append(PositionedPatientSpan(i, span, box))

    used = set()
    groups = []
    for i, part in enumerate(positioned):
        if i in used:
            continue
        group = [part]
        components = set(part.span.components)
        used.add(i)
        while True:
            choices = [
                (abs(other.box[1] - group[-1].box[1]), abs(other.box[0] - group[-1].box[2]), j, other)
                for j, other in enumerate(positioned)
                if j not in used and _adjacent(lines, group[-1], other, components)
            ]
            if not choices:
                break
            _, _, j, following = min(choices, key=lambda choice: choice[:3])
            group.append(following)
            components.update(following.span.components)
            used.add(j)
        if any(item.span.anchored for item in group):
            groups.append(group)
    return groups


def canonical_patient_group(lines, group, name):
    value = ' '.join(lines[item.line].text[item.span.start:item.span.end] for item in group)
    return name.is_canonical(value)
