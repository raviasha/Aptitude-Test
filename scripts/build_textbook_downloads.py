"""Bundle the selected chapter ZIPs without changing their contents."""
import hashlib
import json
from pathlib import Path
import zipfile


def build(root: Path) -> None:
    latest = root / 'question-banks/latest'
    selection = json.loads((latest / 'selection.json').read_text(encoding='utf-8'))
    output = latest / 'textbook-downloads'
    output.mkdir(exist_ok=True)
    groups = [
        ('quantitative-aptitude', 'quantitative-aptitude-chapters-01-39.zip', 39,
         'Extract this master ZIP once. In Coordinator > Question banks, select all chapter ZIPs in chapters/ and click Import. Keep chapter ZIPs zipped. Each chapter is a separate bank. Bulk selection requires a Coordinator build with multi-ZIP upload; older builds import individually.'),
        ('reasoning-source-pages', 'new-reasoning-chapters-01-02-SOURCE-PAGES-ONLY.zip', 2,
         'These two ZIPs contain textbook page images only, NOT importable question banks. Do not upload them to Coordinator. Question extraction and verification are still required.'),
    ]
    for group, name, expected, instructions in groups:
        records = [r for r in selection['packages'] if r['path'].startswith(group + '/')]
        if len(records) != expected:
            raise ValueError(f'{group}: expected {expected} chapters, got {len(records)}')
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
