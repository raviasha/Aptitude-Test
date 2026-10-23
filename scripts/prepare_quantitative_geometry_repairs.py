"""Extract explicitly reviewed diagram boxes; never redraw mathematical figures."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--review-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.review_dir.resolve()
    records = json.loads((root/'inventory.json').read_text(encoding='utf-8'))
    reviewed = json.loads(Path(__file__).with_name('quantitative_diagram_crops.json').read_text())
    media_dir = root/'diagrams'
    media_dir.mkdir(exist_ok=True)
    chapters, pictures = {}, []
    source_hashes = set()
    for record in records:
        question, evidence = record['question'], record['evidence']
        key = question['key']
        if key not in reviewed['crops']:
            raise ValueError(f'Unreviewed diagram: {key}')
        index, box = reviewed['crops'][key]
        source = evidence['question_crops'][index]
        source_bytes = Path(source['path']).read_bytes()
        if digest(source_bytes) != source['sha256']:
            raise ValueError(f'Stale source crop: {key}')
        image = Image.open(source['path']).convert('RGB')
        left, top, right, bottom = box
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError(f'Crop outside source: {key}')
        diagram = image.crop(box)
        exclusions = reviewed.get('excluded_non_diagram_regions', {}).get(key, [])
        for region in exclusions:
            # Explicitly reviewed whitespace/neighboring-answer exclusion only.
            diagram.paste('white', tuple(region))
        target = media_dir/f'{key}.png'
        diagram.save(target)
        original = question['question_text']
        if key == 'ch24-q0163':
            clean = 'In ΔPQR, side PQ = 32 cm and side PR = 25 cm.\n\nWhat is the area of ΔPQR, shown in the figure? (M.B.A., 2011)'
        elif key == 'ch24-q0214':
            clean = original.split('Field book,')[0].rstrip() + '\n' + original[original.index('If the total area'):]
        elif key == 'ch24-q0161':
            clean = original.split('\nThe figure consists')[0]
        else:
            clean = re.split(r'(?:Shared diagram beside[^:]*:|Figure description:|\[Figure:|\bDiagram:|\nFigure:)',original,maxsplit=1)[0].rstrip()
        clean = re.sub(r'^\d+\.\s+', '', clean)
        if key == 'ch24-q0083':
            clean += ' (Hotel Management, 2007)'
        if key == 'ch24-q0316':
            clean = clean.replace('shaded are a', 'shaded area')
        if not clean or clean == original:
            raise ValueError(f'Expected a reviewed text-only description removal: {key}')
        chapter = str(evidence['chapter'])
        entry = chapters.setdefault(chapter, {
            'baseline_sha256': digest(Path(record['archive']).read_bytes()), 'records': [],
        })
        source_hashes.add(evidence['source_pdf_sha256'])
        entry['records'].append({
            'key': key, 'original_text_sha256': digest(original.encode()), 'question_text': clean,
            'media': {'path': str(target), 'sha256': digest(target.read_bytes()),
                      'alt_text': original, 'placement': 'context'},
            'review_note': f"Original diagram visually checked; retained labels/dimensions/shading. Source PDF page {source['page_number']}, crop SHA256 {source['sha256']}, relative box {box}, unrelated non-diagram exclusions {exclusions}. Removed only the matching diagram description; source mathematical claims/answers not corrected.",
        })
        if key == 'ch24-q0163':
            entry['records'][-1]['fallback_text'] = clean + '\nDiagram: P is upper left, Q lower left and R lower right, with a right-angle mark at Q.'
            entry['records'][-1]['review_note'] += ' Fallback excludes the neighboring Q162 prompt, retaining its shared dimensions.'
        if key == 'ch24-q0316':
            entry['records'][-1]['fallback_text'] = original.replace('shaded are a', 'shaded area')
            entry['records'][-1]['review_note'] += ' Corrected extraction spacing: shaded are a -> shaded area, verified against source.'
        pictures.append((key,diagram))
    if len(source_hashes) != 1:
        raise ValueError('Mixed source PDFs')
    review = {'schema_version':1,'source_pdf_sha256':source_hashes.pop(),'chapters':chapters}
    (root/'geometry-review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
    for start in range(0,len(pictures),15):
        sheet = Image.new('RGB',(1100,5*300),'white')
        draw = ImageDraw.Draw(sheet)
        for i,(key,diagram) in enumerate(pictures[start:start+15]):
            x,y=(i%3)*365,(i//3)*300
            draw.text((x+4,y+4),key,fill='red')
            sheet.paste(diagram,(x+4,y+28))
        sheet.save(root/f'diagrams-reviewed-{start//15+1}.png')
    print(f'Prepared {len(pictures)} source-diagram repairs without changing any published ZIP.')


if __name__=='__main__':
    main()
