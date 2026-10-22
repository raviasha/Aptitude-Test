"""Bundle the selected chapter ZIPs without changing their contents."""
import hashlib
import json
import re
from pathlib import Path
import zipfile


def build(root: Path) -> None:
    latest = root / 'question-banks/latest'
    selection = json.loads((latest / 'selection.json').read_text(encoding='utf-8'))
    output = latest / 'textbook-downloads'
    groups = [
        ('quantitative-aptitude', 'quantitative-aptitude-chapters-01-39.zip', 39,
         'Extract this master ZIP once. In Coordinator > Question banks, select all chapter ZIPs in chapters/ and click Import. Keep chapter ZIPs zipped. Each chapter is a separate bank. Bulk selection requires a Coordinator build with multi-ZIP upload; older builds import individually.'),
        ('reasoning-source-pages', 'new-reasoning-chapters-01-02-SOURCE-PAGES-ONLY.zip', 2,
         'These two ZIPs contain textbook page images only, NOT importable question banks. Do not upload them to Coordinator. Question extraction and verification are still required.'),
        ('new-reasoning', 'new-reasoning-chapters-01-41.zip', 41,
         'Coordinator 2.2.2 or newer can import this master ZIP directly. Its limits are 100,000,000 uploaded bytes, 150,000,000 expanded bytes, 10,000 files and 200 chapters; individual chapter ZIPs remain limited to 50,000,000 bytes. In older Coordinator versions, extract this master ZIP once, select the individual chapter ZIPs in chapters/ and click Import. Keep chapter ZIPs zipped. Assign the imported banks to A New Approach to Reasoning in Coordinator; the chapter archives do not contain book_title metadata.'),
    ]
    prepared = []
    # Check every group's counts and names before replacing any master ZIP.
    for group, name, expected, instructions in groups:
        records = [r for r in selection['packages'] if r['path'].startswith(group + '/')]
        if len(records) != expected:
            raise ValueError(f'{group}: expected {expected} chapters, got {len(records)}')
        paths, basenames = set(), set()
        for record in records:
            path = Path(record['path'])
            path_key, basename_key = path.as_posix().casefold(), path.name.casefold()
            if path_key in paths:
                raise ValueError(f'Duplicate selection path: {record["path"]}')
            if basename_key in basenames:
                raise ValueError(f'Duplicate chapter basename in {group}: {path.name}')
            paths.add(path_key)
            basenames.add(basename_key)
        if group == 'new-reasoning':
            identities = [re.fullmatch(r'new_reasoning_ch(\d{2})_.+\.zip',
                                      Path(r['path']).name, re.IGNORECASE) for r in records]
            if any(match is None for match in identities) or {
                int(match[1]) for match in identities if match is not None
            } != set(range(1, 42)):
                raise ValueError('new-reasoning: chapter identities must be exactly 01 through 41')
        prepared.append((name, expected, instructions, records))
    validated = []
    for name, expected, instructions, records in prepared:
        members = {'README.txt': (instructions + '\n').encode(),
                   'selection.json': json.dumps(records, indent=2).encode()}
        for record in records:
            path = latest / record['path']
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record['sha256']:
                raise ValueError(f'Selection hash mismatch: {path}')
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ValueError(f'Corrupt chapter: {path}')
            members['chapters/' + path.name] = data
        validated.append((name, expected, members))
    # Keep the exact validated bytes: a later input failure must not replace
    # earlier masters, and a second read must not introduce unchecked changes.
    output.mkdir(exist_ok=True)
    for name, expected, members in validated:
        destination = output / name
        temporary = destination.with_suffix('.zip.tmp')
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
            for member, data in sorted(members.items()):
                archive.writestr(zipfile.ZipInfo(member, date_time=(2026,10,7,0,0,0)), data)
        with zipfile.ZipFile(temporary) as archive:
            assert archive.testzip() is None
            assert all(archive.read(member) == data for member, data in members.items())
        temporary.replace(destination)
        print(f'{destination.name}: {expected} chapter ZIPs, verified byte-for-byte')


if __name__ == '__main__':
    build(Path(__file__).resolve().parents[1])
