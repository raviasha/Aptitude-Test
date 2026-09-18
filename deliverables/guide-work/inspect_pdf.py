from pathlib import Path
from pypdf import PdfReader
from pdf2image import convert_from_path
root=Path(__file__).resolve().parents[1]
path=root/'KSAT_Department_Installation_Guide_2026-09-10.pdf'
pdf=PdfReader(path)
out=root/'guide-work'/'render'
out.mkdir(exist_ok=True,parents=True)
poppler=r'C:\Users\ravis\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin'
convert_from_path(path,dpi=110,output_folder=out,fmt='png',poppler_path=poppler,output_file='page',paths_only=True,thread_count=2)
for i,page in enumerate(pdf.pages):
    text=page.extract_text()
    print(i+1,len(text),text[121:205].replace('\n',' | '))
print('Pages',len(pdf.pages))
