// Disposable browser smoke test; no student database or live server is used.
// node tests/client_media_browser.cjs <complete-question.png> <screenshot.png>
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.KSAT_PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const root = path.resolve(__dirname, '..');
  const png = await fs.readFile(process.argv[2]);
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1024, height: 768 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const id = '11111111-1111-4111-8111-111111111111';
    const media = { url: `assets/${'a'.repeat(64)}.png`, alt_text: 'Directions and complete matrix question', width: 648, height: 285 };
    const question = { question_id: 7, source_key: 'ch441-q0001',
      question_text: 'Duplicated matrix description', options: { A: 'Duplicated option description', B: 'Text-only answer' },
      display_media: { question: media, options: { A: media } } };
    const attempt = { attempt_id: id, state: 'in_progress', remaining_seconds: 600,
      current_question_id: 7, questions: [question], responses: {} };
    const review = { attempt_id: id, test_name: 'Image display check',
      questions: [{ question, selected_answer: 'A', correct_answer: 'A', solution_steps: ['A retained solution.'] }] };
    let state = 'acknowledged_result';
    let broken = false;
    await page.routeWebSocket('**/api/lifecycle/presence', socket => socket.send(JSON.stringify({ state: 'connected' })));
    await page.addInitScript(() => sessionStorage.setItem('ksat-browser-session-active', 'true'));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;
      if (pathname.startsWith('/api/')) {
        if (pathname.includes('/assets/')) {
          await route.fulfill(broken ? { status: 404, body: '' } : { contentType: 'image/png', body: png });
          return;
        }
        const result = { attempt_id: id, test_name: 'Image display check', score: 1,
          total_questions: 1, percentage: 100, review_state: 'available' };
        const data = pathname === '/api/state' ? { state, attempt: state === 'in_progress' ? attempt : result }
          : pathname === '/api/reviews' ? { reviews: [result] }
          : pathname === `/api/reviews/${id}` ? review
          : pathname === '/api/assessments' ? { assessments: [] }
          : pathname.endsWith('/review') ? review
          : pathname.endsWith('/result') ? { result }
          : pathname.endsWith('/browser-heartbeat') ? { state: 'in_progress' }
          : pathname.endsWith('/violations') ? { violations: 1 }
          : pathname === `/api/attempts/${id}` ? (state === 'in_progress' ? attempt : { state, result, attempt_id: id })
          : null;
        assert.ok(data, `Unexpected test API ${pathname}`);
        await route.fulfill({ json: data });
        return;
      }
      const relative = pathname === '/' ? 'client/index.html' : pathname.slice(1);
      const target = path.resolve(root, 'static', relative);
      assert.ok(target.startsWith(path.join(root, 'static') + path.sep));
      try {
        await route.fulfill({ body: await fs.readFile(target), contentType:
          ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' })[path.extname(target)] || 'application/octet-stream' });
      } catch { await route.fulfill({ status: 404, body: '' }); }
    });
    await page.goto('http://127.0.0.1:47111/');
    await page.getByRole('button', { name: 'Review answers', exact: true }).click();
    await page.locator('.review-question > img').waitFor();
    await page.waitForFunction(() => document.querySelector('.review-question > p').hidden);
    assert.equal(await page.getByText('Duplicated matrix description', { exact: true }).isVisible(), false);
    assert.equal(await page.getByText('Duplicated option description', { exact: true }).isVisible(), false);
    assert.equal(await page.getByText('A retained solution.', { exact: true }).isVisible(), true);
    await page.screenshot({ path: process.argv[3], fullPage: true });

    state = 'in_progress';
    await page.reload();
    await page.locator('#enter-fullscreen').click();
    await page.waitForFunction(() => document.querySelector('#question-text').hidden);
    assert.equal(await page.locator('#question-text').isVisible(), false);
    question.display_media.question = { ...media, placement: 'context' };
    await page.waitForFunction(() => !document.querySelector('#question-text').hidden);
    question.display_media.question = { ...media, url: `assets/${'b'.repeat(64)}.png` };
    broken = true;
    await page.waitForFunction(() => !document.querySelector('#question-text').hidden &&
      document.querySelector('#question-media img').complete && !document.querySelector('#question-media img').naturalWidth);
    assert.equal(await page.locator('#question-text').isVisible(), true);
    // Context charts use the legacy-compatible stimulus wire shape. Their stem
    // remains visible, and a failed image reveals the complete transcription.
    question.source_key = 'ch38-q0001';
    question.question_text = 'Study the chart. What percentage is shaded?';
    delete question.display_media;
    question.stimulus = { id: 'chart-1', type: 'image',
      url: `assets/${'c'.repeat(64)}.png`, alt_text: 'Chart transcription: shaded = 25%; unshaded = 75%.' };
    broken = false;
    await page.waitForFunction(() => document.querySelector('#question-text').textContent.includes('What percentage'));
    await page.waitForFunction(() => [...document.querySelectorAll('img')].some(img => img.src.includes('cccc') && img.naturalWidth));
    assert.equal(await page.getByText(question.question_text, { exact: true }).isVisible(), true);
    assert.equal(await page.locator('.media-transcript').first().isVisible(), false);
    await page.screenshot({ path: process.argv[3].replace(/\.png$/, '-context-live.png'), fullPage: true });
    broken = true;
    question.stimulus.url = `assets/${'d'.repeat(64)}.png`;
    await page.waitForFunction(() => [...document.querySelectorAll('.media-transcript')].some(node => !node.hidden));
    assert.equal(await page.getByText(question.question_text, { exact: true }).isVisible(), true);
    assert.equal(await page.getByText(question.stimulus.alt_text, { exact: true }).isVisible(), true);
    state = 'acknowledged_result';
    await page.reload();
    await page.getByRole('button', { name: 'Review answers', exact: true }).click();
    await page.waitForFunction(() => [...document.querySelectorAll('.media-transcript')].some(node => !node.hidden));
    assert.equal(await page.getByText(question.question_text, { exact: true }).isVisible(), true);
    assert.equal(await page.getByText(question.stimulus.alt_text, { exact: true }).isVisible(), true);
    await page.screenshot({ path: process.argv[3].replace(/\.png$/, '-context-fallback.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: review, live test, replacement/context images and broken-image fallback at 1024x768');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
