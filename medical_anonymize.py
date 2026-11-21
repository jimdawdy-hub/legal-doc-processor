"""Plan document-wide synthetic replacements from positioned medical text."""
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import hashlib
import hmac
import json
import re
import unicodedata

from medical_dates import calendar_spans, shift_dates
from patient_names import patient_names, positioned_patient_spans
from pdf_ocr import Line, Page, Word


PEOPLE = {'patient', 'physician', 'staff', 'family', 'person'}
ORGANIZATIONS = {'facility', 'insurer', 'organization'}
ROLES = PEOPLE | ORGANIZATIONS | {'address', 'contact', 'identifier', 'date'}
HUMAN_TITLE = re.compile(r"^(?:(?:Dr|Mr|Ms|Mrs|Miss|Prof)\.?\s+)+", re.I)
CREDENTIAL = re.compile(
    r"[\s,(/]+(?:M\.?D\.?|D\.?O\.?|P\.?A\.?(?:-C)?|R\.?N\.?|L\.?P\.?N\.?|"
    r"P\.?T\.?|DPT|PTA|OTR/?L|PT/DPT|D\.?C\.?|N\.?P\.?(?:-C)?|APRN|"
    r"MPH|Ph\.?D\.?|FNP(?:-C)?|LCSW|MSW|JD|Esq\.?|Jr\.?|Sr\.?|II|III)\b\.?\)?\s*$",
    re.I,
)
PERSON_WORD = re.compile(r"[^\W\d_][^\W\d_.'’\-]*\.?", re.UNICODE)
NAME_SUFFIX = re.compile(
    r'\b(?:hospital|hosp\.?|clinic|institute|(?:medical|health|surgery|imaging|rehabilitation|'
    r'outpatient|radiology|therapy|care|pain|spine|orthop\w*|neurol\w*) (?:center|centre|system|'
    r'group|associates|services)|health(?:care)? system|'
    r'(?:health|insurance|benefit) (?:plan|network|company)|blue (?:cross|shield))\b', re.I,
)
INSURER = re.compile(r'\b(?:insurance|health plan|blue (?:cross|shield)|payer|benefit network)\b', re.I)
SKIP_PHRASE = re.compile(
    r'\b(?:chief complaint|physical exam|history of|review of|assessment and|'
    r'range of motion|drug screen|blood pressure|progress note|patient name|date of birth|'
    r'page \d|diagnosis|signature on file|insurance company|physical therapy|'
    r'occupation(?:al)? therapy|emergency department|patient portal|medical record|'
    r'plan of care|visit summary|family history|social history|no known|medication list|'
    r'patient information|follow up|billed amount|date of service|not applicable|'
    r'allergies|acetaminophen|ibuprofen|lidocaine|oxycodone|hydrocodone|sodium|'
    r'methylprednisolone|metoprolol|prednisone|'
    r'lumbar spine|cervical spine|right shoulder|left shoulder|upper extremity|lower extremity|'
    r'radiculopathy|osteoarthritis|hypertension|diabetes mellitus|noncontrast|no fault)\b', re.I,
)
EMPTY_VALUES = {'na', 'none', 'unknown', 'null', 'spouse', 'mother', 'father', 'daughter', 'son',
                'friend', 'brother', 'sister', 'self', 'patient', 'male', 'female', 'person',
                'name', 'other', 'signed', 'electronic', 'done', 'yes', 'no', 'do', 'md', 'rn',
                'office', 'entity', 'medical', 'not available', 'see above', 'prov',
                'signature', 'facility', 'provider', 'address', 'telephone', 'initials'}
BILLING = re.compile(r'(?<![\w./])(?:[$][ \t]*[(-]?[ \t]*\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*\.\d{2})'
                     r'(?![\w./])')
DURATION = re.compile(r'\b\d+(?:[–-]\d+)?[ -]*(?:days?|weeks?|months?|years?|hours?|minutes?|'
                      r'mg|mcg|mL|tablets?|units?)\b', re.I)
CONTACT = re.compile(
    r'(?<![\w@])(?:[\w.%+\-]+@[\w.-]+\.[a-z]{2,}|(?:https?://|www\.)[^\s<>]{3,120})|'
    r'(?<![\d/])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s*|\d{3}[\s.-])\d{3}[\s.-]\d{4}'
    r'(?:\s*(?:ext\.?|x|#)\s*\d{1,8})?(?![\d/])', re.I,
)
KNOWN_PHONE = re.compile(r'(?:\+?1[ \t.-]*)?\(?\d{3}\)?[ \t.-]*\d{3}[ \t.-]*\d{4}')
ADDRESS = re.compile(
    r'(?<![\w/-])(?:\d{1,6}\s+(?:(?:[NSEW]\.?)\s+)?(?:[\w\'’.-]+[ \t]+){1,7}'
    r'(?:street|st|avenue|ave|aven|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|'
    r'circle|cir|place|pl|parkway|pkwy|highway|hwy|terrace|ter|way|trail|trl|'
    r'square|sq|pike|loop)\.?\b(?:[ ,]+(?:apt|suite|ste|unit|fl|#)\.?\s*#?\w{1,8})?'
    r'|P\.?\s*O\.?\s*Box\s+\d{1,8})', re.I,
)
CITY_STATE_ZIP = re.compile(
    r"(?<![A-Za-z])(?:[A-Z][A-Za-z.'’-]{1,20} )?[A-Z][A-Za-z.'’-]{1,20},?[ \t]+"
    r"(?:[A-Z]{2})\s+\d{5}(?:-\d{4})?(?![\d/-])",
)
SSN = re.compile(r'(?<![\w/])(?:\d{3}|[*Xx]{3})[- ](?:\d{2}|[*Xx]{2})[- ]\d{4}(?![\d/])')
ID_FIELD = re.compile(
    r'\b(?:M[ .]?R[ .]?N|medical record (?:number|no\.?|id|#)|NPI|'
    r'(?:patient|member|subscriber|claim|policy|account|encounter|invoice|case|visit|'
    r'chart|order|referral|authorization|accession|record|certificate|reference|'
    r'group|provider|insurance|transaction|tax|patient control|'
    r'guarantor|registration)\s*(?:number|no\.?|num\.?|id|#)|'
    r'SSN|social security(?: number)?|driver.?s license)\s*[:#.=–-]*\s*'
    r'(?P<value>[A-Z0-9*]+(?:[-./][A-Z0-9*]+){0,4})\b', re.I,
)
LABEL = re.compile(
    r'\b(?:(?P<patient>patient(?:\s+(?:full\s+)?name)?|name\s+of\s+(?:the\s+)?patient|'
    r'insured(?:\s+name)?|subscriber\s+name|member\s+name|guarantor(?:\s+name)?)'
    r'|(?P<physician>(?:(?:attending|treating|ordering|referring|rendering|'
    r'consulting|reading|supervising|billing|requesting|requesting and supervising|'
    r'primary care)\s+)?(?:physician|provider|doctor)(?:\s+name)?|'
    r'PCP|referred by|ordered by|reviewed by|interpreted by|signed by|'
    r'electronically signed(?: and verified)? by|electronically authenticated by)'
    r'|(?P<staff>technologist|nurse|therapist|scribe|dictated by|transcribed by|entered by|created by)'
    r'|(?P<family>emergency contact(?: name)?|parent(?: name)?|guardian(?: name)?|'
    r'spouse(?: name)?|next of kin|responsible party(?: name)?)'
    r'|(?P<facility>facility(?: name)?|hospital(?: name)?|clinic(?: name)?|'
    r'billing entity|place of service(?: name)?)'
    r'|(?P<insurer>(?:(?:primary|secondary|tertiary|health)\s+)?insurance(?: company| carrier| name)?|'
    r'carrier(?: name)?|pay(?:e|o)r(?: name)?|health plan)'
    r'|(?P<organization>employer(?: name)?)'
    r'|(?P<address>(?:home |mailing |street |office |bill to )?address(?:\s*[12])?))'
    r'\s*(?:(?P<separator>[:=])\s*|(?=$))', re.I,
)
INLINE_LABEL = re.compile(
    r'\s+(?:(?:patient|provider|facility|member|insurance|account|chart|claim)\s+(?:id|no\.?|#|name)'
    r'|date(?: of (?:birth|service))?|dob|age|gender|sex|address|tel|phone|fax|'
    r'status|encounter|mrn|npi|time|attending|referring|spouse|type|relation(?:ship)?|'
    r'policy|discharge|admit|location|visit|insurance|signature)\s*[:#=]', re.I,
)
TITLED = re.compile(
    r"(?<!\w)(?:Dr|Mr|Mrs|Ms|Miss)\.?\s+"
    r"(?P<name>[A-Z][\w'’.-]*(?:[ \t]+[A-Z][\w'’.-]*){0,2})",
)
ROLE_PRIORITY = {'date': 120, 'contact': 108, 'identifier': 105, 'address': 102,
                 'patient': 96, 'physician': 94, 'family': 93, 'staff': 92,
                 'facility': 90, 'insurer': 90, 'organization': 89, 'person': 80}
OCR_CONFUSABLES = str.maketrans({'0': 'o', '1': 'l', '5': 's', '8': 'b'})


@dataclass(frozen=True)
class Candidate:
    start: int
    end: int
    role: str
    trust: int
    replacement: str | None = None


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    role: str
    replacement: str


def read_transcript(path):
    with path.open(encoding='utf-8') as stream:
        for row in stream:
            record = json.loads(row)
            record['lines'] = tuple(
                Line(line['text'], tuple(line['box']),
                     tuple(Word(w['text'], tuple(w['box'])) for w in line['words']),
                     line.get('confidence'))
                for line in record['lines']
            )
            yield Page(**record)


def _norm(value):
    value = unicodedata.normalize('NFKD', value.casefold())
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in value if not unicodedata.combining(c))))


def _person_canonical(value):
    value = HUMAN_TITLE.sub('', value).strip()
    while (end := CREDENTIAL.search(value)) is not None:
        value = value[:end.start()].strip()
    if ',' in value:
        last, rest = value.split(',', 1)
        if last.strip() and rest.strip():
            value = f'{rest.strip()} {last.strip()}'
    # Dots in initials and OCR splitting a hyphen do not create a distinct name.
    return _norm(value.replace('.', '').replace("'", '').replace('’', ''))


def canonical(value, role):
    if role in PEOPLE:
        return _person_canonical(value)
    if role == 'contact':
        if '@' in value or '/' in value:
            return value.strip().casefold()
        digits = re.sub(r'\D', '', value)
        return digits[1:] if len(digits) in (11, 12, 13, 14) and digits.startswith('1') else digits
    if role == 'identifier':
        return ''.join(re.findall(r'\w', value.casefold()))
    value = _norm(value)
    if role in ORGANIZATIONS:
        value = re.sub(r'^(?:the )| (?:inc|llc|llp|ltd|pllc|pc|corp)$', '', value)
        value = re.sub(r'\b(?:st|hosp|ctr)\b', lambda m: {'st':'saint','hosp':'hospital','ctr':'center'}[m[0]],
                       value)
    return value


def patient_surname(aliases):
    """Read the surname from the confirmed patient record, with a compound-name override."""
    if aliases['role'] != 'patient':
        return None
    if 'identity' in aliases:
        return aliases['identity']['surname']
    if not aliases['values']:
        return None
    if 'surname' in aliases:
        value = aliases['surname']
        if not isinstance(value, str) or not re.fullmatch(
            r"[^\W\d_]+(?:[ .’'-]+[^\W\d_]+)*", value.strip()
        ):
            raise ValueError('Invalid confirmed patient surname.')
        return value.strip()
    name = HUMAN_TITLE.sub('', aliases['values'][0]).strip()
    while (suffix := CREDENTIAL.search(name)) is not None:
        name = name[:suffix.start()].strip()
    first_part, separator, second_part = name.partition(',')
    if separator and first_part.strip() and second_part.strip():
        return first_part.strip()
    parts = name.split()
    return parts[-1] if len(parts) > 1 else None


def alias_expression(value):
    tokens = re.findall(r'[^\W_]+', value)
    return r'[\W_]*'.join(map(re.escape, tokens)) if len(''.join(tokens)) >= 2 else None


def _compact_name(value):
    return ''.join(_norm(value).split())


def _tighten(text, candidate):
    if candidate.replacement is not None:
        return candidate
    raw = text[candidate.start:candidate.end]
    value = raw.strip(' \t|:;="“”[]')
    start = candidate.start + (raw.find(value) if value else 0)
    if candidate.role in PEOPLE:
        title = HUMAN_TITLE.match(value)
        if title:
            start += title.end()
            value = value[title.end():]
        while (suffix := CREDENTIAL.search(value)) is not None:
            value = value[:suffix.start()]
        value = value.rstrip(' .,;')
    return replace(candidate, start=start, end=start + len(value))


def _usable(text, candidate):
    value = text[candidate.start:candidate.end]
    norm = _norm(value)
    if not 2 <= len(value) <= 120 or not norm or norm in EMPTY_VALUES:
        return False
    if candidate.role == 'identifier':
        return 4 <= len(norm.replace(' ', '')) <= 32 and any(c.isdigit() for c in value)
    if candidate.role == 'address':
        return candidate.trust >= 80 or ADDRESS.search(value) is not None
    if candidate.role == 'contact':
        return CONTACT.fullmatch(value) is not None or (
            candidate.trust >= 80 and KNOWN_PHONE.fullmatch(value) is not None)
    if candidate.role in PEOPLE | ORGANIZATIONS:
        if any(c in value for c in ':$=|?') or not any(c.isalpha() for c in value):
            return False
        if candidate.role in PEOPLE and (not PERSON_WORD.search(value) or
                                         len(norm.split()) > 6 or norm.isdigit()):
            return False
        if candidate.role in PEOPLE and re.search(r'\d{2,}', value):
            return False
        if SKIP_PHRASE.search(value) and not (candidate.role in ORGANIZATIONS and
                                             candidate.trust >= 80 and NAME_SUFFIX.search(value)):
            return False
    return True


def _field_value(text, start, end):
    value = text[start:end]
    stops = [m.start() for pattern in (INLINE_LABEL, re.compile(r'\s+[|;]|\t|\s{3,}|\s+\(?\d{1,4}[/.:]\d'))
             if (m := pattern.search(value)) is not None]
    return start + min(stops, default=len(value))


def _same_row(label, value):
    x0, y0, x1, y1 = label.box
    a0, b0, _, b1 = value.box
    return -2 <= a0 - x1 <= 175 and abs((y0 + y1 - b0 - b1) / 2) <= max(3, (y1 - y0) * 0.57)


def _direct(page):
    for i, line in enumerate(page.lines):
        text = line.text
        for role, pattern in (('contact', CONTACT), ('address', ADDRESS), ('address', CITY_STATE_ZIP),
                              ('identifier', SSN)):
            for found in pattern.finditer(text):
                yield i, Candidate(*found.span(), role, 100)
        for found in ID_FIELD.finditer(text):
            yield i, Candidate(*found.span('value'), 'identifier', 99)
        labels = list(LABEL.finditer(text))
        for j, label in enumerate(labels):
            role = next(name for name, value in label.groupdict().items()
                        if value is not None and name != 'separator')
            if not label['separator'] and (j or label.start() > 4):
                continue
            start = label.end()
            next_field = next((other for other in labels[j + 1:] if other['separator']), None)
            end = next_field.start() if next_field else len(text)
            end = _field_value(text, start, end)
            if label['separator'] and start < end and text[start:end].strip():
                yield i, Candidate(start, end, role, 92)
            if next_field or text[start:end].strip():
                continue
            # Many scanned forms split the label and its same-row value into separate boxes.
            matches = [(key, other) for key, other in enumerate(page.lines)
                       if key != i and _same_row(line, other) and not LABEL.search(other.text)]
            if matches:
                key, other = min(matches, key=lambda entry: entry[1].box[0])
                yield key, Candidate(0, _field_value(other.text, 0, len(other.text)), role, 86)
        for found in TITLED.finditer(text):
            role = 'physician' if found.group().casefold().startswith('dr') else 'person'
            yield i, Candidate(*found.span('name'), role, 76)


def _model_candidates(nlp, pages):
    inputs = ((line.text, (page.number, i)) for page in pages for i, line in enumerate(page.lines)
              if len(line.text) > 2 and any(c.isalpha() for c in line.text))
    for doc, location in nlp.pipe(inputs, as_tuples=True, batch_size=128):
        for entity in doc.ents:
            value = entity.text
            if entity.label_ == 'PERSON':
                role = 'person'
                if not PERSON_WORD.search(value) or not any(c.isupper() for c in value):
                    continue
                if len(_norm(value).split()) < 2:
                    continue
            elif entity.label_ in ('ORG', 'FAC') and (NAME_SUFFIX.search(value) or INSURER.search(value)):
                role = 'insurer' if INSURER.search(value) else 'facility'
            elif entity.label_ in ('GPE', 'LOC') and len(_norm(value)) >= 4:
                # A standalone place name in a form's address block can identify the record.
                if not ADDRESS.search(doc.text) and not CITY_STATE_ZIP.search(doc.text):
                    continue
                role = 'address'
            else:
                continue
            yield location, Candidate(entity.start_char, entity.end_char, role, 63)


def read_model_candidates(paths):
    for path in paths:
        with path.open(encoding='utf-8') as stream:
            for row in stream:
                record = json.loads(row)
                for line, start, end, role in record['candidates']:
                    if role in ROLES - {'date'}:
                        yield (record['page'], line), Candidate(start, end, role, 69)


def _choose(text, candidates):
    protected = [m.span() for regex in (BILLING, DURATION) for m in regex.finditer(text)]
    dates = calendar_spans(text) if any(item.role == 'patient' for item in candidates) else ()
    accepted = []
    for item in sorted(candidates, key=lambda c: (-c.trust, -ROLE_PRIORITY[c.role],
                                                  c.start - c.end, c.start)):
        if not 0 <= item.start < item.end <= len(text):
            continue
        if item.role != 'date' and item.replacement is None and not _usable(text, item):
            continue
        if item.role != 'date' and any(a < item.end and item.start < b for a, b in protected):
            continue
        if item.role == 'patient' and any(a <= item.start and item.end <= b for a, b in dates):
            continue
        if not any(prior.start < item.end and item.start < prior.end for prior in accepted):
            accepted.append(item)
    return sorted(accepted, key=lambda c: c.start)


def _near_name(first, second):
    a, b = first.split(), second.split()
    if len(a) != len(b):
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        return len(shorter) == 2 and shorter == [longer[0], longer[-1]]
    if min(len(first), len(second)) < 5 or abs(len(first) - len(second)) > 6:
        return False
    if len(a) == len(b) and min(len(a), len(b)) >= 2:
        # Shared initials are supporting evidence only alongside a full matching name part.
        if a[0] == b[0] or a[-1] == b[-1]:
            common, other = (0, -1) if a[0] == b[0] else (-1, 0)
            if len(a[common]) >= 3 and (min(len(a[other]), len(b[other])) == 1 and
                                        a[other][0] == b[other][0]):
                return True
            if len(a[common]) >= 3 and SequenceMatcher(None, a[other], b[other]).ratio() >= .76:
                return True
    return first.translate(OCR_CONFUSABLES) == second.translate(OCR_CONFUSABLES)


def _near_org(first, second):
    if min(len(first), len(second)) < 12:
        return False
    a, b = first.split(), second.split()
    return (len(a) == len(b) and (a[0] == b[0] or a[-1] == b[-1]) and
            SequenceMatcher(None, first, second).ratio() >= .91)


class Registry:
    """Each canonical source identity and its observed aliases share one synthetic value."""

    def __init__(self, observations, key, faker):
        self.key = key
        self.faker = faker
        self.clusters = {}
        self.aliases = defaultdict(set)
        self.values = {}
        self.used = {normalized for _, normalized in observations}
        self.ambiguous = set()
        self.patient_surnames = {}
        typed = defaultdict(set)
        for role, norm in observations:
            if role in PEOPLE - {'person'}:
                typed[norm].add(role)
        for role, norm in sorted(observations, key=lambda x: (x[0] == 'person', len(x[1].split()) == 1,
                                                             -observations[x], x)):
            if not norm:
                continue
            original_role = role
            if role == 'person' and len(typed[norm]) == 1:
                role = next(iter(typed[norm]))
            if (role, norm) in self.clusters:
                cluster = self.clusters[role, norm]
            else:
                source_matches = set()
                for previous_role, other in set(self.clusters.values()):
                    same_people = role in PEOPLE and previous_role in PEOPLE and (
                        role == previous_role or role == 'person')
                    same_orgs = role in ORGANIZATIONS and previous_role == role
                    if same_people and (norm == other or _near_name(norm, other) or
                                        (' ' not in norm and len(norm) >= 3 and other.split()[-1] == norm)):
                        source_matches.add((previous_role, other))
                    if same_orgs and _near_org(norm, other):
                        source_matches.add((previous_role, other))
                if len(source_matches) > 1:
                    self.ambiguous.add((role, norm))
                cluster = next(iter(source_matches)) if len(source_matches) == 1 else (role, norm)
                self.clusters[role, norm] = cluster
            self.clusters[original_role, norm] = cluster
            self.aliases[norm].add(cluster)
        self.known_surnames = defaultdict(set)
        for cluster in set(self.clusters.values()):
            role, norm = cluster
            if role in PEOPLE and len(norm.split()) > 1:
                self.known_surnames[norm.split()[-1]].add(cluster)

    def add_aliases(self, spellings):
        for variants in spellings:
            role, values = variants['role'], variants.get('values', [])
            if not values and 'identity' in variants:
                identity = variants['identity']
                values = [f"{identity['given']} {identity['surname']}"]
            if role not in ROLES - {'date'} or len(values) < 1:
                raise ValueError('Invalid private entity alias record.')
            anchor = canonical(values[0], role)
            cluster = self.clusters.get((role, anchor), (role, anchor))
            for raw in values:
                norm = canonical(raw, role)
                if norm:
                    prior = self.clusters.get((role, norm))
                    if prior and prior != cluster:
                        for observed, linked in self.clusters.items():
                            if linked == prior:
                                self.clusters[observed] = cluster
                                aliases = self.aliases[observed[1]]
                                aliases.discard(prior)
                                aliases.add(cluster)
                    self.clusters[role, norm] = cluster
                    self.aliases[norm].add(cluster)
                    self.used.add(norm)
            if surname := patient_surname(variants):
                key = _compact_name(surname)
                if key and self.patient_surnames.setdefault(key, cluster) != cluster:
                    self.patient_surnames[key] = None

    def display(self, raw, role):
        value = self.value(raw, role)
        if role == 'patient' and _compact_name(raw) in self.patient_surnames:
            return value.rsplit(' ', 1)[-1]
        return value

    def value(self, raw, role):
        norm = canonical(raw, role)
        cluster = self.clusters.get((role, norm), (role, norm))
        if role == 'patient' and (key := _compact_name(raw)) in self.patient_surnames:
            cluster = self.patient_surnames[key] or (role, norm)
        if cluster in self.values:
            return self.values[cluster]
        digest = hmac.digest(self.key, repr(cluster).encode(), 'sha256')
        self.faker.seed_instance(int.from_bytes(digest, 'little'))
        role, anchor = cluster
        attempts = 0
        while True:
            attempts += 1
            if role in PEOPLE:
                first, last = self.faker.first_name(), self.faker.last_name()
                if _compact_name(last) in self.patient_surnames:
                    continue
                result = f'{first} {last}'
                if attempts <= 30 and len(result) > max(10, len(anchor) + 1):
                    continue
            elif role in ORGANIZATIONS:
                if role == 'insurer':
                    suffix = 'Health Plan'
                elif role == 'facility':
                    if 'hospital' in anchor:
                        suffix = 'Hospital'
                    elif 'therapy' in anchor or 'rehab' in anchor:
                        suffix = 'Rehabilitation Center'
                    elif any(term in anchor for term in ('imaging', 'radiology')):
                        suffix = 'Imaging Center'
                    elif 'surgery' in anchor:
                        suffix = 'Surgery Center'
                    elif 'clinic' in anchor or len(anchor) < 15:
                        suffix = 'Clinic'
                    else:
                        suffix = 'Medical Center'
                else:
                    suffix = 'Services'
                result = f'{self.faker.last_name()} {suffix}'
            elif role == 'address':
                if re.search(r'\b[a-z]{2} \d{5}(?: \d{4})?$', anchor):
                    result = f'{self.faker.city()}, {self.faker.state_abbr()} {self.faker.zipcode()}'
                elif anchor.startswith('p o box ') or anchor.startswith('po box '):
                    result = f'PO Box {self.faker.random_int(100, 999)}'
                elif not any(c.isdigit() for c in anchor):
                    result = self.faker.city()
                else:
                    result = f'{self.faker.building_number()} {self.faker.last_name()} St'
                if attempts <= 30 and len(result) > max(10, len(anchor) + 2):
                    continue
            elif role == 'contact':
                if '@' in raw:
                    result = f'{self.faker.user_name()}@example.org'
                elif '/' in raw or raw.lower().startswith('www.'):
                    result = f'https://example.org/{digest.hex()[:6]}'
                else:
                    result = f'{self.faker.random_int(200, 999)}-555-01{self.faker.random_int(0, 99):02}'
            else:
                result = ''.join(str(self.faker.random_digit()) if c.isdigit() else
                                 self.faker.random_uppercase_letter() if c.isalpha() else c for c in raw)
            norm = canonical(result, role)
            if norm and norm not in self.used:
                self.used.add(norm)
                self.values[cluster] = result
                return result


def _alias_rules(registry, spellings):
    rules = {}
    for norm, clusters in registry.aliases.items():
        if len(clusters) != 1 or len(norm.split()) < 2:
            continue
        cluster, = clusters
        if cluster[0] in PEOPLE | ORGANIZATIONS:
            # Also find case and punctuation variants absent from the first-pass model.
            ordered = re.split(r'\s+', norm)
            options = {tuple(ordered)}
            if cluster[0] in PEOPLE and len(ordered) == 2:
                reverse = tuple(reversed(ordered))
                key = (cluster[0], ' '.join(reverse))
                if not registry.aliases.get(key[1]) or registry.aliases[key[1]] == {cluster}:
                    options.add(reverse)
                    registry.clusters.setdefault(key, cluster)
            for tokens in options:
                expression = r'[ \t,.\-]+'.join(map(re.escape, tokens))
                rules[expression] = (cluster[0], 73)
    for surname, clusters in registry.known_surnames.items():
        if len(clusters) == 1 and len(surname) >= 3:
            cluster, = clusters
            role = cluster[0]
            expression = rf'(?<=Dr\. )(?i:{re.escape(surname)})'
            registry.clusters[role, surname] = cluster
            rules[expression] = (role, 82)
    explicit = {}
    for row in spellings:
        role = row['role']
        if role == 'patient' and 'identity' in row:
            continue
        for value in row['values']:
            explicit[re.escape(value)] = (role, 112)
        if (surname := patient_surname(row)) and (expression := alias_expression(surname)):
            # A complete name in a bound provider or family field keeps its own role.
            rules[expression] = ('patient', 78)
    return _compile_rules(rules), _compile_rules(explicit)


def _observed_rules(registry, observations):
    rules = {}
    for (role, value), trust in observations.items():
        norm = canonical(value, role)
        cluster = registry.clusters.get((role, norm))
        if not cluster or len(registry.aliases[norm]) != 1:
            continue
        if role == 'identifier':
            if len(norm) < 8:
                continue
        elif role == 'contact':
            if len(norm) < 7:
                continue
        elif role in PEOPLE | ORGANIZATIONS:
            if len(norm.split()) < 2 and (trust < 90 or len(norm) < 5 or role in PEOPLE):
                continue
        elif role == 'address' and not any(c.isdigit() for c in norm):
            continue
        rules[re.escape(value)] = (role, 81)
        if role == 'identifier' and value != norm:
            expression = r'[-. /]*'.join(map(re.escape, norm))
            rules.setdefault(expression, (role, 81))
        if role == 'contact' and len(norm) == 10 and norm.isdecimal():
            number = rf'\(?{norm[:3]}\)?[ \t.-]*{norm[3:6]}[ \t.-]*{norm[6:]}'
            rules.setdefault(rf'(?:\+?1[ \t.-]*)?{number}', (role, 81))
    return _compile_rules(rules)


def _compile_rules(rules):
    groups = []
    expressions = sorted(rules, key=len, reverse=True)
    for start in range(0, len(expressions), 200):
        batch = expressions[start:start + 200]
        compiled = re.compile(r'(?<!\w)(?:' + '|'.join(f'(?P<m{i}>{x})'
                              for i, x in enumerate(batch)) + r')(?!\w)', re.I)
        groups.append((compiled, [rules[x] for x in batch]))
    return groups


def find_aliases(text, rules):
    for pattern, roles in rules:
        for found in pattern.finditer(text):
            role, trust = roles[int(found.lastgroup[1:])]
            yield Candidate(*found.span(), role, trust)


def _patient_name_candidates(page, identities, candidates):
    for name in identities:
        for group in positioned_patient_spans(page.lines, name):
            if len(group) == 1:
                member = group[0]
                span = member.span
                # A partial match inside a distinctly labeled clinician or family
                # name still belongs to that person's full replacement.
                if span.components <= {'given', 'middle', 'surname'} and any(
                    item.role in PEOPLE - {'patient', 'person'} and item.trust >= 85
                    and item.start <= span.start and span.end <= item.end
                    and item.end - item.start > span.end - span.start
                    for item in candidates.get((page.number, member.line), ())
                ):
                    continue
            target = max(group, key=lambda member: member.span.end - member.span.start)
            for member in group:
                span = member.span
                yield member.line, Candidate(span.start, span.end, 'patient', 118,
                                             name.replacement if member is target else '')


def make_plan(pages, *, days, key, faker, nlp=None, extras=(), spellings=(), date_corrections=()):
    """Return line/span replacements, the shared registry and a text-free count receipt."""
    candidates = defaultdict(list)
    date_edits = {}
    receipt = Counter()
    texts = {(page.number, i): line.text for page in pages for i, line in enumerate(page.lines)}
    identities = patient_names(spellings)
    for page in pages:
        for i, item in _direct(page):
            candidates[page.number, i].append(item)
        for i, item in _patient_name_candidates(page, identities, candidates):
            candidates[page.number, i].append(item)
        for i, line in enumerate(page.lines):
            found, unresolved = shift_dates(line.text, days)
            receipt['unresolved_date_expressions'] += unresolved
            for value in found:
                if (value.shifted - value.original).days != days:
                    raise RuntimeError('Date shift verification failed.')
                location = (page.number, i)
                date_edits[location, value.start, value.end] = value
                candidates[location].append(Candidate(value.start, value.end, 'date', 120))
    for location, value in date_corrections:
        if (value.shifted - value.original).days != days:
            raise RuntimeError('Independent date shift verification failed.')
        date_edits[location, value.start, value.end] = value
        candidates[location].append(Candidate(value.start, value.end, 'date', 119))
        receipt['independent_date_corrections'] += 1
    if nlp is not None:
        for location, item in _model_candidates(nlp, pages):
            candidates[location].append(item)
    for location, item in extras:
        if location in texts:
            candidates[location].append(item)
    observations = Counter()
    spellings_seen = {}
    for location, items in candidates.items():
        text = texts[location]
        for item in _choose(text, [_tighten(text, x) for x in items if 0 <= x.start < x.end <= len(text)]):
            if item.role != 'date' and item.replacement is None:
                value = text[item.start:item.end]
                observations[item.role, canonical(value, item.role)] += item.trust
                spellings_seen[item.role, value] = max(item.trust, spellings_seen.get((item.role, value), 0))
    registry = Registry(observations, key, faker)
    registry.add_aliases(spellings)
    learned, explicit = _alias_rules(registry, spellings)
    observed = _observed_rules(registry, spellings_seen)
    result = {}
    for location, text in texts.items():
        items = [*candidates.get(location, ()), *find_aliases(text, learned),
                 *find_aliases(text, observed), *find_aliases(text, explicit)]
        chosen = _choose(text, [_tighten(text, x) for x in items if 0 <= x.start < x.end <= len(text)])
        edits = []
        for item in chosen:
            original = text[item.start:item.end]
            if item.role == 'date':
                replacement = date_edits[location, item.start, item.end].replacement
            elif item.replacement is not None:
                replacement = item.replacement
            else:
                replacement = registry.display(original, item.role)
            edits.append(Edit(item.start, item.end, item.role, replacement))
            receipt[f'{item.role}_replacements'] += 1
        expected = apply_edits(text, edits)
        if BILLING.findall(text) != BILLING.findall(expected) and not any(
            item.role == 'date' and BILLING.search(text[item.start:item.end]) for item in edits
        ):
            raise RuntimeError('A proposed replacement changes a protected amount.')
        if edits:
            result[location] = edits
    receipt['ambiguous_aliases'] = len(registry.ambiguous)
    receipt['unique_replacements'] = len(registry.values)
    receipt['unique_replacements_by_role'] = dict(Counter(role for role, _ in registry.values))
    receipt['edited_lines'] = len(result)
    return result, registry, dict(receipt)


def apply_edits(text, edits, *, begin=0, end=None):
    end = len(text) if end is None else end
    value = text[begin:end]
    for edit in reversed(edits):
        value = value[:edit.start - begin] + edit.replacement + value[edit.end - begin:]
    return value


def input_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
