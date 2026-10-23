"""Read-only inventory/contact sheets for reviewing textbook presentation repairs."""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records, pictures = [], []
    latest = Path(__file__).resolve().parents[1] / 'question-banks/latest/quantitative-aptitude'
    for archive_path in sorted(latest.glob('*.zip')):
        with zipfile.ZipFile(archive_path) as archive:
            questions = [json.loads(line) for name in archive.namelist()
                         if name.startswith('questions/') and name.endswith('.jsonl')
                         for line in archive.read(name).decode().splitlines() if line.strip()]
            for question in questions:
                if question.get('display_media') or not re.search(
                    r'\b(diagram|figure description|adjoining figure|following figure|shaded region|as shown|given figure)\b',
                    question['question_text'], re.I,
                ):
                    continue
                chapter, number = map(int, re.fullmatch(r'ch(\d+)-q(\d+)', question['key']).groups())
                evidence_path = args.workspace / f'tmp/textbook-v2/chapter-{chapter:03d}/source-evidence/ch{chapter:03d}-q{number:04d}.json'
                evidence = json.loads(evidence_path.read_text(encoding='utf-8'))['payload']
                records.append({'archive': str(archive_path), 'question': question, 'evidence': evidence})
                for index, crop in enumerate(evidence['question_crops']):
                    crop_path = Path(crop['path'])
                    assert hashlib.sha256(crop_path.read_bytes()).hexdigest() == crop['sha256']
                    image = Image.open(crop_path).convert('RGB')
                    if image.width > 680:
                        image.thumbnail((680, 1000))
                    pictures.append((f"{question['key']} segment {index} page {crop['page_number']}", image))
    for start in range(0, len(pictures), 6):
        group = pictures[start:start + 6]
        row_heights = [max(image.height for _, image in group[i:i+2]) + 34 for i in range(0,len(group),2)]
        sheet = Image.new('RGB', (1400, sum(row_heights)), 'white')
        draw = ImageDraw.Draw(sheet)
        y = 0
        for row in range(0,len(group),2):
            for col, (label, image) in enumerate(group[row:row+2]):
                x = col * 700
                draw.text((x+8,y+5), label, fill='red')
                sheet.paste(image,(x+8,y+30))
            y += row_heights[row//2]
        sheet.save(args.output / f'geometry-{start//6+1:02d}.png')
    (args.output/'inventory.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'{len(records)} questions, {len(pictures)} source segments; {args.output}')


if __name__ == '__main__':
    main()
