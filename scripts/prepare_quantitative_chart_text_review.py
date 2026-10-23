"""Read-only review/verification helper for the explicit ch36--39 text manifest.

Never rewrites packages. The manifest, rather than these review heuristics, is
the authority for packaging. UTF-8 text hashes pin every edit to its input.
"""
import argparse
import collections
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name('quantitative_chart_text_review.json')
REVIEWED_CORPUS_SHA256 = '336e5aa70d3b252100e99a51113010c9841166a32726eaa8ab6a485f5884b608'

# Explicit selections from the complete unique-prefix inspection for each media
# group. Indices refer to --inspect output, not printed question numbers. Only
# these reviewed prefix lines survive; every question's own stem is preserved.
# The emitted manifest is checked in explicitly and is the packaging input.
REVIEW = {
    'ch36-q0001': ([2, 3, 11, 16, 17], 'Candidate table; retain units and App./Qual. definitions.'),
    'ch36-q0006': ([1, 3, 13], 'Semester fees; retain course context and thousands-of-rupees units.'),
    'ch36-q0011': ([1, 2, 14], 'Entrance examination; retain N/P definitions and lakh units.'),
    'ch36-q0016': ([1, 2, 5, 16], 'Entrance examination; retain lakh units and passing/failing ratio meaning.'),
    'ch36-q0021': ([1, 2, 11], 'Employees; retain department and female/male context.'),
    'ch36-q0026': ([1, 2, 13], 'School enrolment; retain hundreds units; both tabular and slash-form rows removed.'),
    'ch36-q0031': ([1, 4, 9, 26], 'Production/sales graph and ratio table; retain both explanatory sentences and tonnes units.'),
    'ch36-q0036': ([1, 3, 4], 'Athletes; retain hundreds units and M/F definitions.'),
    'ch36-q0041': ([1, 2], 'Company expenditure; retain lakh-rupee and annual context.'),
    'ch36-q0046': ([1, 2], 'Examination candidates; retain appeared/qualified/selected context.'),
    'ch36-q0052': ([1, 2, 23], 'Marks; retain percentage interpretation and maximum-marks bracket note.'),
    'ch36-q0057': ([1, 10], 'Poverty-line table; retain population/sex interpretation.'),
    'ch36-q0061': ([2, 9, 10], 'Revenue; retain full net-revenue formula and missing-value instructions verbatim.'),
    'ch36-q0066': ([1, 2, 10, 11, 12], 'Batsmen; retain strike-rate formula, all-matches assumption and missing-value instructions verbatim.'),
    'ch36-q0071': ([1, 2, 10], 'Exports; retain crore units and Profit = Exports - Imports definition in original typography.'),
    'ch36-q0076': ([1, 2], 'Printers; retain page/day context.'),
    'ch36-q0081': ([1], 'Publishing houses; retain directions and complete individual prompt.'),
    'ch37-q0001': ([1, 3, 13], 'Pearl exports; retain crore-rupee context.'),
    'ch37-q0006': ([1, 2, 4, 20], 'School pie/bar pair; retain total 6000 and boys-series meaning; inline q0008 boundary reviewed separately.'),
    'ch37-q0011': ([1, 3, 11, 19], 'Vehicle graph/table; retain hours, kilometres and two-day meanings.'),
    'ch37-q0016': ([1, 3, 12], 'Scholarship students; retain appeared/qualified meaning and rounding directions in prompt.'),
    'ch37-q0021': ([1, 2], 'Paper production; retain lakh-tonne units and original directions.'),
    'ch37-q0028': ([1], 'Raw-material and finished-goods graphs; retain complete explanatory directions and lakh-rupee units.'),
    'ch37-q0032': ([1, 2], 'Fertilizer production; retain 10000-tonne unit.'),
    'ch37-q0037': ([1], 'Company expenditure; retain percentage-distribution explanation.'),
    'ch37-q0042': ([1, 2, 3, 5, 7, 14], 'Teacher pie/bar pair; retain total 4500 and series meanings, including inline headings.'),
    'ch37-q0047': ([1, 3, 20], 'Specializations; retain thousands units and complete prompt conditions.'),
    'ch37-q0052': ([1, 2, 3, 4, 15], 'Car production; retain year-specific totals 350000/440000; remove both direct and cumulative transcription variants.'),
    'ch37-q0057': ([1], 'Demand/production; retain explanatory directions and source wording of individual prompts.'),
    'ch37-q0063': ([1], 'Book sales; conservatively retain complete explanatory directions, including their repeated sales description.'),
    'ch37-q0067': ([1, 2], 'Monthly income; retain thousands-of-rupees units.'),
    'ch37-q0072': ([1, 2, 3, 10], 'Language marks; retain out-of-100 maximum and language context.'),
    'ch37-q0077': ([1, 2], 'Company income/expenditure; retain year and crore-rupee units.'),
    'ch38-q0001': ([1, 2, 11, 12], 'Renovation; retain estimated total 120000 and complete discount conditions.'),
    'ch38-q0005': ([1, 2, 3, 5], 'Mobile phones; retain total 45000 and company-ratio meaning.'),
    'ch38-q0010': ([1, 2, 4, 12, 20], 'Lecturers; retain total 1600 and male/female ratio meaning.'),
    'ch38-q0015': ([1, 2], 'Food crops; retain acreage context and original directions.'),
    'ch38-q0020': ([1, 3, 5, 7], 'Graduate/post-graduate charts; retain both level-specific totals; q0023 printed label 24 reviewed without changing prompt.'),
    'ch38-q0025': ([1, 2], 'Human-body diagrams; remove fractions/percentages duplicated in images; preserve complete prompt.'),
    'ch38-q0030': ([1, 2, 3, 5], 'MBA specializations; retain total 8000.'),
    'ch38-q0035': ([1], 'Sports expenditure; retain complete explanatory directions.'),
    'ch38-q0040': ([1, 2, 3, 5, 7, 10, 11], 'MBA entrants/passes; retain distinct totals 8550/5700, including inline variants.'),
    'ch38-q0045': ([1, 3, 4, 6, 7, 9, 12, 13], 'College streams; retain both populations, totals 3500/1500 and original girls-distribution wording.'),
    'ch38-q0050': ([1, 3, 4, 11], 'Family expenditure; retain degreewise interpretation and total 45800.'),
    'ch38-q0054': ([1, 2, 8, 9, 15], 'Laptop charts; retain Dell/all-laptop populations and totals 2400/4500; source chart errors are not corrected.'),
    'ch38-q0059': ([1, 2, 3, 4, 5, 6], 'Expenditure; retain total 60000 and all four Roman-numeral category definitions; source image visually checked.'),
    'ch38-q0063': ([1, 2, 3, 9, 10], 'Health clubs; retain total/male populations and totals 6300/3600; source percentages are not corrected.'),
    'ch38-q0068': ([1, 2, 9, 10], 'Cellular phones; retain total 11200, October context and Nokia/Samsung ratio meaning.'),
    'ch38-q0073': ([1, 2], 'Club membership; remove numeric transcription only; source chart values are not corrected.'),
    'ch39-q0001': ([1, 3, 9, 17], 'Aptitude-test students; retain thousands units.'),
    'ch39-q0006': ([1, 3, 12, 20], 'Production/sales; retain tonnes units and complete explanation of A:B ratios.'),
    'ch39-q0011': ([1, 3, 4, 5, 6, 11, 12, 23], 'Electronic goods; retain all original profit, income and investment formulas verbatim.'),
    'ch39-q0019': ([1, 2, 9], 'Girls enrolment; retain thousands units.'),
    'ch39-q0024': ([1, 2, 18, 19], 'Exports graph/table; retain explicit repeated-X/source-Z discrepancy note; packaged image visually checked.'),
    'ch39-q0029': ([1, 3, 23, 24], 'Company profits; retain profit/loss formula and any prompt-level definition verbatim.'),
    'ch39-q0034': ([1, 2, 21], 'Trees; retain NGO/state context; do not correct source values.'),
    'ch39-q0039': ([1, 2, 12], 'Import/export ratios; retain full 2011 label-versus-position discrepancy note; packaged image visually checked.'),
    'ch39-q0044': ([1, 2], 'Calories; retain treadmill/day/person context.'),
    'ch39-q0049': ([1, 2], 'Watches; retain brand/town/month context.'),
    'ch39-q0054': ([1, 2, 17, 20], 'Profits; retain source-table no-header/discrepancy prose where present; graph and printed table visually checked; do not reconcile 35 versus 25.'),
    'ch39-q0059': ([1, 2], 'Projects; retain full governmental/non-governmental definitions in prompts.'),
    'ch39-q0064': ([1, 2, 4, 12], 'Visas; retain missing-month-label and missing-table-header notes; image visually checked; no months inferred.'),
}


def clean_labels(text):
    text = re.sub(r'\[(?:IBPS|RBI|SSC|Gramin Bank|United India|IDBI)[^\n]*\]', '', text)
    text = re.sub(r'\(\s*(?:Bank P\.O\.|S\.S\.C\.)[^\n)]*\)', '', text)
    text = re.sub(r'(?m)^Ex(?:\.\s*|\s+)(?:\d+\.\s*)?', '', text)
    text = re.sub(r'(?m)^(Directions?)\s*\(Questions?[^)]*\):\s*', r'\1: ', text)
    text = re.sub(r'(?m)^\(Questions?[^)]*\):\s*', '', text)
    return '\n'.join(line.rstrip() for line in text.splitlines()).strip()


def reviewed_entries(qs):
    assert corpus_sha256(qs) == REVIEWED_CORPUS_SHA256, 'Input changed: re-review before using prefix selections.'
    groups = collections.defaultdict(list)
    for q in qs:
        groups[q['display_media']['question']['sha256']].append(q)
    assert len(groups) == len(REVIEW) == 62
    entries = []
    for media_hash, group in groups.items():
        first = group[0]['key']
        indices, note = REVIEW[first]
        unique = list(dict.fromkeys(line.strip() for q in group for line in split_stem(q)[0].splitlines() if line.strip()))
        keep = {unique[i] for i in indices}
        partial = {}
        if first == 'ch37-q0006':
            line = unique[20]
            partial[line] = line[:line.index(' School A 12%')] + '\n' + line[line.index('Number of Boys'):line.index(': A =')]
        if first == 'ch37-q0042':
            for i in (2, 3):
                partial[unique[i]] = unique[i].split(':', 1)[0]
        if first == 'ch38-q0040':
            for i in (10, 11):
                partial[unique[i]] = unique[i].split(':', 1)[0]
        for q in group:
            prefix, stem = split_stem(q)
            kept = [partial.get(line.strip(), line.strip()) for line in prefix.splitlines() if line.strip() in keep]
            new_prefix = clean_labels('\n'.join(kept))
            new_stem = clean_labels(stem)
            text = new_prefix + '\n\n' + new_stem
            special = {
                'ch36-q0001': ' Original malformed zone wording retained; no source correction.',
                'ch36-q0005': ' Original missing year retained; no value inferred.',
                'ch37-q0061': ' Original repeated wording in prompt retained.',
                'ch38-q0023': ' Original prompt has label 24; label removed without renumbering or changing the question.',
            }.get(q['key'], '')
            entries.append({
                'key': q['key'],
                'original_text_sha256': hashlib.sha256(q['question_text'].encode('utf-8')).hexdigest(),
                'question_text': text,
                'review_note': f'Reviewed media {media_hash}. Removed duplicated numeric chart/table transcription and exercise/question/source labels. {note} Required directions and full prompt retained.{special}',
            })
    return entries


def corpus_sha256(qs):
    return hashlib.sha256(json.dumps(qs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def records():
    result = []
    for path in sorted((ROOT / 'question-banks/latest/quantitative-aptitude').glob('ch3[6-9]*zip')):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith('questions/') and name.endswith('.jsonl'):
                    for line in archive.read(name).decode('utf-8').splitlines():
                        if line.strip():
                            q = json.loads(line)
                            media = q['display_media']['question']
                            assert hashlib.sha256(archive.read(media['asset'])).hexdigest() == media['sha256']
                            result.append(q)
    return result


def printed_number(q):
    ch, n = map(int, re.fullmatch(r'ch(\d+)-q(\d+)', q['key']).groups())
    breaks = {36: [0, 25, 60], 37: [0, 31, 56], 38: [0, 29, 53], 39: [0, 28, 43]}
    return n - max(b for b in breaks[ch] if n > b)


def split_stem(q):
    text = q['question_text']
    number = printed_number(q)
    if q['key'] == 'ch38-q0023':
        number = 24  # Existing source transcription label; do not renumber content.
    pattern = rf'(?m)^{number}\.\s+'
    if q['key'] == 'ch37-q0008':
        pattern = r'(?<= )8\.\s+'
    matches = list(re.finditer(pattern, text))
    if matches:
        match = matches[-1]
        return text[:match.start()], text[match.end():]
    # Used only to propose a review boundary; the printed inspection must be
    # reviewed before generating the explicit manifest, including all exceptions.
    lines = list(re.finditer(r'(?m)^.*\S.*$', text))
    candidates = [m for m in lines if re.match(
        r'(?:What |Which |How |In |If |The |For |By |Out |Total |During |From )', m.group())]
    assert candidates, q['key']
    match = candidates[-1]
    return text[:match.start()], text[match.start():]


def inspect(qs, chapter):
    groups = collections.defaultdict(list)
    for q in qs:
        if int(q['chapter']) == chapter:
            groups[q['display_media']['question']['sha256']].append(q)
    for h, group in groups.items():
        print('\nGROUP', group[0]['key'], h)
        unique = list(dict.fromkeys(line.strip() for q in group for line in split_stem(q)[0].splitlines() if line.strip()))
        for i, line in enumerate(unique):
            print(f'{i}: {line}')
        print('STEMS:')
        for q in group:
            print(q['key'], repr(split_stem(q)[1]))


def verify(qs):
    assert corpus_sha256(qs) == REVIEWED_CORPUS_SHA256, 'Reviewed package records changed.'
    entries = json.loads(MANIFEST.read_text(encoding='utf-8'))
    assert isinstance(entries, list)
    by_key = {entry['key']: entry for entry in entries}
    assert len(entries) == len(by_key) == len(qs) == 306
    assert set(by_key) == {q['key'] for q in qs}
    groups = {q['display_media']['question']['sha256'] for q in qs}
    assert len(groups) == 62
    total_before = total_after = 0
    protected_lines = 0
    source_citation = re.compile(r'\[(?:IBPS|RBI|SSC|Gramin Bank|United India|IDBI)[^\n]*\]')
    protected = re.compile(r'^(?:Note:|II\. Few|\(ii\)|\(iii\)|Percent Profit =|Profit Earned =|− \(Total Investment|Profit or Loss %|Source graph detail:|Only the first two|\(The (?:printed table|graph plots))')
    for q in qs:
        entry = by_key[q['key']]
        assert set(entry) == {'key', 'original_text_sha256', 'question_text', 'review_note'}
        assert entry['original_text_sha256'] == hashlib.sha256(q['question_text'].encode('utf-8')).hexdigest(), q['key']
        assert entry['question_text'].strip() and entry['review_note'].strip()
        assert entry['question_text'] != q['question_text'], q['key']
        # Independently remove only the source citation from the reviewed stem;
        # the complete remaining prompt must survive byte-for-byte, including
        # source errors, rounding conditions, formulas and all numbers.
        prefix, stem = split_stem(q)
        expected_stem = source_citation.sub('', stem).strip()
        assert entry['question_text'].endswith('\n\n' + expected_stem), q['key']
        for line in prefix.splitlines():
            if protected.match(line):
                assert line in entry['question_text'], (q['key'], line)
                protected_lines += 1
        # The entire visible result is a deletion-only subsequence of source
        # tokens; no words, values or mathematical punctuation are invented.
        original_tokens = iter(re.findall(r'\w+|[^\w\s]', q['question_text']))
        for token in re.findall(r'\w+|[^\w\s]', entry['question_text']):
            assert any(old == token for old in original_tokens), (q['key'], token)
        assert not re.search(r'(?im)^(?:EXERCISE\b|\[Source:|Ex\.?\s+\d|\d+\.\s)', entry['question_text']), q['key']
        assert '|' not in entry['question_text'], q['key']
        simulated = copy.deepcopy(q)
        simulated['question_text'] = entry['question_text']
        assert {k: v for k, v in simulated.items() if k != 'question_text'} == {k: v for k, v in q.items() if k != 'question_text'}
        assert all(simulated[k] == q[k] for k in ('options', 'correct_answer', 'solution_steps', 'explanation', 'display_media'))
        total_before += len(q['question_text'])
        total_after += len(entry['question_text'])
    assert entries == reviewed_entries(qs), 'Explicit manifest differs from reviewed selections.'
    assert 'Total number of students = 6000.' in by_key['ch37-q0008']['question_text']
    assert 'Out of 4500 Teachers' in by_key['ch37-q0042']['question_text']
    for number in range(59, 63):
        for definition in ('I. Raw materials', 'II. Conveyance', 'III. Electricity', 'IV. Overhead expenses'):
            assert definition in by_key[f'ch38-q{number:04d}']['question_text']
    print(json.dumps({'records': len(entries), 'media_groups': len(groups), 'chapter_counts': dict(collections.Counter(q['chapter'] for q in qs)), 'characters_before': total_before, 'characters_after': total_after, 'complete_stems_preserved': len(entries), 'protected_note_formula_lines_preserved': protected_lines, 'deletion_only_token_check': True, 'reviewed_corpus_sha256': corpus_sha256(qs), 'non_text_fields_unchanged': True}, indent=2))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', type=int, choices=range(36, 40))
    parser.add_argument('--emit-reviewed', action='store_true', help='Print the reviewed candidate JSON; never write packages.')
    args = parser.parse_args()
    qs = records()
    if args.emit_reviewed:
        print(json.dumps(reviewed_entries(qs), ensure_ascii=False, indent=2))
    elif args.inspect:
        inspect(qs, args.inspect)
    else:
        verify(qs)


if __name__ == '__main__':
    main()
