from pathlib import Path
import hashlib
import json
from pypdf import PdfReader
from pdf2image import convert_from_path

work = Path(__file__).resolve().parent
source = work.parent/'KSAT_Department_Installation_Guide_2026-09-10_Timer_Fix.pdf'
pages = PdfReader(source).pages
assert len(pages) == 18, len(pages)
text = '\n'.join(page.extract_text() for page in pages)
assert 'd6509f72c93dfa024c09e6d7419e648e8d5a8f1f' in text
assert '4c41d30' not in text
release = Path(r'C:\Users\ravis\AppData\Local\Temp\KSATFacultyTimer-d6509f7-20260910\release')
for line in (release/'SHA256SUMS.txt').read_text().splitlines():
    assert line.split()[0] in text.replace('\n','')
output = work/'render-timer'
output.mkdir(exist_ok=True)
paths = convert_from_path(source,dpi=110,output_folder=output,fmt='png',poppler_path=r'C:\Users\ravis\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin',output_file='timer',paths_only=True,thread_count=2)
old = sorted((work/'render').glob('*.png'))
assert len(old) == 18, len(old)
changed = []
for index, (new, previous) in enumerate(zip(paths,old),1):
    if hashlib.sha256(Path(new).read_bytes()).digest() != hashlib.sha256(previous.read_bytes()).digest():
        changed.append({'page':index,'path':str(new)})
print(json.dumps({'page_count':len(pages),'changed':changed},indent=2))
