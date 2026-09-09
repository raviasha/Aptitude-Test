'use strict';

// Execute the shipping browser script with controlled DOM, network, and timer boundaries.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.copy = '';
    this.classList = { add() {} };
  }
  get textContent() { return this.copy + this.children.map(child => child.textContent).join(' '); }
  set textContent(value) { this.copy = String(value); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  append(...children) { this.children.push(...children); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute() {}
  focus() {}
  querySelectorAll(selector) {
    const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
    if (selector === 'button[data-answer]') {
      return descendants.filter(child => child.tagName === 'button' && child.dataset.answer);
    }
    if (selector === 'button') return descendants.filter(child => child.tagName === 'button');
    return descendants;
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const finished = {
  attempt_id: '11111111-1111-4111-8111-111111111111',
  test_name: 'First test', score: 1, total_questions: 1, percentage: 100,
  review_state: 'available',
};
const launch = {
  release_id: 'second-release', test_name: 'Second test', content_ready: true,
};
const attempt = {
  attempt_id: '22222222-2222-4222-8222-222222222222',
  state: 'in_progress', remaining_seconds: 600, current_question_id: 7,
  questions: [{ question_id: 7, question_text: 'Second test question', options: { A: 'One', B: 'Two' } }],
  responses: {},
};
const pendingReviewPath = `/api/attempts/${finished.attempt_id}/review`;
const localReview = {
  attempt_id: finished.attempt_id, test_name: finished.test_name,
  local_result: { score: 1, total_questions: 1, attempted: 1, percentage: 100 },
  upload_pending: true, result: null,
  questions: [{
    question: { question_id: 7, question_text: 'Review-only question', options: { A: 'One', B: 'Two' } },
    selected_answer: 'A', correct_answer: 'A', solution_steps: ['Review-only solution step'],
  }],
};

function failure(code, retryable = true) {
  const error = new Error(code);
  error.problem = { code, retryable };
  return error;
}

async function boot(initialState = 'waiting_or_ready', routes = {}) {
  const elements = new Map();
  const timers = new Map();
  const calls = [];
  let timerId = 0;
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element(id));
      return elements.get(id);
    },
    querySelector: () => ({ content: 'csrf' }),
    createElement: name => new Element(name),
    addEventListener() {},
    fullscreenElement: null,
    documentElement: {
      async requestFullscreen() { document.fullscreenElement = this; },
    },
    async exitFullscreen() { document.fullscreenElement = null; },
  };
  const window = {
    sessionStorage: { getItem: () => 'true', setItem() {} },
    addEventListener() {},
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback, delay) { timers.set(++timerId, { callback, delay, interval: true }); return timerId; },
    clearInterval(id) { timers.delete(id); },
  };
  const handlers = {
    '/api/state': () => ({ state: initialState, attempt: initialState === 'waiting_or_ready' ? null : finished }),
    [`/api/attempts/${finished.attempt_id}`]: () => ({ state: initialState, result: finished, attempt_id: finished.attempt_id }),
    '/api/assessments': () => ({ assessments: [launch] }),
    '/api/reviews': () => ({ reviews: [finished] }),
    '/api/assessments/second-release/start': () => attempt,
    '/api/logout': () => ({ state: 'login' }),
    [pendingReviewPath]: () => { throw failure('review_not_released', false); },
    ...routes,
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
    document, window, AbortController,
    async fetch(path, options) {
      calls.push({ path, options });
      assert.ok(handlers[path], `Unexpected request ${path}`);
      // Deliberately allow late resolution after abort to verify view ownership too.
      const payload = await handlers[path](options);
      return { ok: true, status: 200, json: async () => payload };
    },
  }, { filename: process.argv[2] });
  const flush = async () => {
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
  };
  const actions = () => elements.get('action-area');
  const getButton = label => actions().querySelectorAll('*').find(
    element => element.tagName === 'button' && element.textContent === label,
  );
  const click = async label => {
    const target = getButton(label);
    assert.ok(target, `Button "${label}" must be available. Current screen: ${actions().textContent}`);
    assert.equal(target.disabled, false, `Button "${label}" must be usable`);
    const pending = target.listeners.get('click')({ preventDefault() {} });
    await flush();
    return { pending };
  };
  const tick = async delay => {
    for (const [id, timer] of [...timers]) {
      if (timer.delay !== delay || !timers.has(id)) continue;
      if (!timer.interval) timers.delete(id);
      timer.callback();
    }
    await flush();
  };
  await flush();
  return { elements, actions, getButton, click, tick, flush, calls, handlers, timers };
}

const scenarios = {
  async login_shows_supported_departments() {
    const page = await boot('login');
    const form = page.actions().children.find(child => child.tagName === 'form');
    const selects = form.querySelectorAll('*').filter(child => child.tagName === 'select');
    assert.equal(selects.length, 1);
    assert.equal(selects[0].value, 'AIML');
    assert.deepEqual(
      selects[0].children.map(option => option.textContent),
      ['CSE', 'AIML', 'CS&D', 'CCE', 'CSE (ICB)', 'ECE', 'ME', 'MCA', 'Science and Humanities'],
    );
    assert.equal(page.elements.get('rights-notice').hidden, false);
  },
  async pending_review_waits_for_faculty_close() {
    const page = await boot('sealed_pending');
    assert.ok(page.getButton('Check review availability'));
    await page.tick(5000);
    assert.doesNotMatch(page.actions().textContent, /Review-only question|100%/);
    assert.equal(page.getButton('Sign out'), undefined);
    assert.equal(page.getButton('Back to assessments'), undefined);
    page.handlers[pendingReviewPath] = () => localReview;
    await page.tick(5000);
    assert.match(page.actions().textContent, /Locally calculated score.*1 \/ 1 \(100%\)/);
    assert.match(page.actions().textContent, /Review-only question/);
    assert.match(page.actions().textContent, /Review-only solution step/);
    assert.match(page.actions().textContent, /Your choice/);
    assert.match(page.actions().textContent, /Correct choice/);
    assert.match(page.actions().textContent, /upload.*background|Upload pending/i);
    assert.ok(page.getButton('Back to submission'));
    assert.equal(page.getButton('Sign out'), undefined);
    assert.equal(page.getButton('Back to assessments'), undefined);
  },
  async slow_pending_review_does_not_delay_upload_receipt() {
    const pending = deferred();
    const page = await boot('sealed_pending', { [pendingReviewPath]: () => pending.promise });
    await page.click('Check review availability');
    await page.tick(5000);
    assert.equal(page.calls.filter(call => call.path === pendingReviewPath).length, 1);
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => ({
      state: 'acknowledged_result', attempt_id: finished.attempt_id, result: finished,
    });
    await page.tick(1000);
    assert.equal(page.elements.get('status-title').textContent, 'Result received');
    pending.resolve(localReview);
    await page.flush();
    assert.equal(page.elements.get('status-title').textContent, 'Result received');
    assert.doesNotMatch(page.actions().textContent, /Review-only question/);
  },
  async local_review_receipt_updates_without_replacing_questions() {
    const page = await boot('sealed_pending', { [pendingReviewPath]: () => localReview });
    await page.click('Check review availability');
    const list = page.actions().children.find(child => child.className === 'review-list');
    assert.ok(list, 'A faculty-authorized local review must open before upload completes');
    list.scrollTop = 135;
    await page.tick(1000);
    assert.equal(page.actions().children.find(child => child.className === 'review-list'), list);
    assert.match(page.actions().textContent, /Locally calculated score.*1 \/ 1/);
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => ({
      state: 'acknowledged_result', attempt_id: finished.attempt_id,
      result: { ...finished, score: 0, percentage: 0 },
    });
    await page.tick(1000);
    assert.equal(page.actions().children.find(child => child.className === 'review-list'), list);
    assert.equal(list.scrollTop, 135);
    assert.match(page.actions().textContent, /Confirmed score.*0 \/ 1 \(0%\)/);
    assert.match(page.actions().textContent, /Upload confirmed/i);
    assert.ok(page.getButton('Back to assessments'));
    assert.ok(page.getButton('Sign out'));
    assert.equal(page.getButton('Back to submission'), undefined);
    assert.equal([...page.timers.values()].some(timer => timer.delay === 1000), false);
  },
  async local_review_reports_failed_upload_without_unlocking_navigation() {
    const page = await boot('sealed_pending', { [pendingReviewPath]: () => localReview });
    await page.click('Check review availability');
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => ({
      state: 'faculty_intervention_required', attempt_id: finished.attempt_id,
      message: 'Faculty must resolve the upload.', queue: { status: 'faculty_intervention_required' },
    });
    await page.tick(1000);
    assert.match(page.actions().textContent, /Faculty.*(?:attention|assistance|resolve)/i);
    assert.match(page.actions().textContent, /Review-only question/);
    assert.equal(page.getButton('Sign out'), undefined);
    assert.equal(page.getButton('Back to assessments'), undefined);
    await page.click('Back to submission');
    assert.equal(page.elements.get('status-title').textContent, 'Faculty assistance required');
    assert.equal(page.calls.some(call => call.path === '/api/logout'), false);
  },
  async pending_review_back_ignores_late_receipt_and_does_not_reopen_itself() {
    const page = await boot('sealed_pending', { [pendingReviewPath]: () => localReview });
    await page.click('Check review availability');
    const receipt = deferred();
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => receipt.promise;
    await page.tick(1000);
    await page.click('Back to submission');
    receipt.resolve({ state: 'acknowledged_result', attempt_id: finished.attempt_id, result: finished });
    await page.flush();
    assert.equal(page.elements.get('status-title').textContent, 'Assessment submitted');
    await page.tick(5000);
    assert.equal(page.elements.get('status-title').textContent, 'Assessment submitted');
    assert.ok(page.getButton('Review answers'));
    assert.equal(page.getButton('Sign out'), undefined);
  },
  async new_signin_cannot_reuse_previous_session_review_grant() {
    const page = await boot('sealed_pending', { [pendingReviewPath]: () => localReview });
    await page.click('Check review availability');
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => ({
      state: 'acknowledged_result', attempt_id: finished.attempt_id, result: finished,
    });
    await page.tick(1000);
    await page.click('Sign out');
    page.handlers['/api/login'] = () => ({ state: 'waiting_or_ready' });
    const form = page.actions().children.find(child => child.tagName === 'form');
    const inputs = form.querySelectorAll('*').filter(child => child.tagName === 'input');
    inputs[0].value = 'S200';
    inputs[1].value = 'student-password';
    await form.listeners.get('submit')({ preventDefault() {} });
    // Reusing a local identifier must never carry authorization across a new login.
    page.handlers['/api/assessments/second-release/start'] = () => ({
      state: 'sealed_pending', attempt_id: finished.attempt_id,
    });
    page.handlers[pendingReviewPath] = () => { throw failure('review_not_released', false); };
    await page.click('Start');
    assert.equal(page.getButton('Review answers'), undefined);
    assert.ok(page.getButton('Check review availability'));
    await page.click('Check review availability');
    assert.doesNotMatch(page.actions().textContent, /Review-only question|Review-only solution/);
  },
  async result_to_next_test() {
    const page = await boot('acknowledged_result');
    const pendingReview = deferred();
    page.handlers['/api/reviews'] = () => pendingReview.promise;
    await page.tick(5000);
    assert.match(page.actions().textContent, /1 \/ 1 \(100%\)/);
    await page.click('Back to assessments');
    assert.match(page.actions().textContent, /Second test/);
    await page.click('Start');
    pendingReview.resolve({ reviews: [finished] });
    await page.flush();
    assert.equal(page.elements.get('assessment-panel').hidden, false);
    assert.equal(page.elements.get('question-text').textContent, 'Second test question');
    assert.equal(page.calls.some(call => ['/api/login', '/api/logout'].includes(call.path)), false);
  },
  async slow_reviews_do_not_block_launches() {
    const slowReview = deferred();
    const page = await boot('waiting_or_ready', { '/api/reviews': () => slowReview.promise });
    assert.ok(page.getButton('Start'), 'Launched tests must render before slow review responses');
    await page.tick(5000);
    assert.equal(page.calls.filter(call => call.path === '/api/reviews').length, 1);
    assert.equal(page.calls.filter(call => call.path === '/api/assessments').length, 2);
    slowReview.resolve({ reviews: [finished] });
    await page.flush();
    assert.match(page.actions().textContent, /First test/);
  },
  async launched_tests_refresh_with_closed_reviews() {
    let rows = [];
    const page = await boot('waiting_or_ready', { '/api/assessments': () => ({ assessments: rows }) });
    assert.match(page.actions().textContent, /First test/);
    rows = [launch];
    await page.tick(5000);
    assert.match(page.actions().textContent, /Second test/);
    const nextRefresh = deferred();
    page.handlers['/api/assessments'] = () => nextRefresh.promise;
    await page.click('Refresh');
    await page.tick(5000);
    assert.equal(page.calls.filter(call => call.path === '/api/assessments').length, 3,
      'A slow refresh must not start overlapping assessment requests');
    nextRefresh.reject(new Error('temporary outage'));
    await page.flush();
    assert.ok(page.getButton('Start'), 'A transient refresh failure must preserve usable tests');
    page.handlers['/api/assessments'] = () => ({ assessments: [] });
    await page.tick(5000);
    assert.equal(page.getButton('Start'), undefined, 'A later successful refresh must replace stale tests');
  },
  async late_lists_cannot_replace_exam() {
    const lateAssessments = deferred();
    const lateReviews = deferred();
    const page = await boot('waiting_or_ready', {
      '/api/reviews': () => ({ reviews: [{ ...finished, review_state: 'waiting' }] }),
    });
    page.handlers['/api/assessments'] = () => lateAssessments.promise;
    page.handlers['/api/reviews'] = () => lateReviews.promise;
    await page.tick(5000);
    await page.click('Start');
    lateAssessments.resolve({ assessments: [] });
    lateReviews.resolve({ reviews: [finished] });
    await page.flush();
    assert.equal(page.elements.get('assessment-panel').hidden, false);
    assert.equal(page.elements.get('status-panel').hidden, true);
    assert.equal(page.elements.get('question-text').textContent, 'Second test question');
    assert.equal([...page.timers.values()].some(timer => timer.delay === 5000), false);
  },
  async late_review_cannot_replace_signin() {
    const pending = deferred();
    const page = await boot('acknowledged_result', { '/api/reviews': () => pending.promise });
    await page.tick(5000);
    await page.click('Sign out');
    pending.resolve({ reviews: [finished] });
    await page.flush();
    assert.equal(page.elements.get('status-title').textContent, 'Student sign in');
    assert.ok(page.getButton('Sign in'));
    assert.equal([...page.timers.values()].some(timer => timer.delay === 5000), false);
  },
  async review_navigation_owns_screen() {
    const page = await boot('waiting_or_ready', {
      [`/api/reviews/${finished.attempt_id}`]: () => ({ ...finished, questions: [] }),
      '/api/reviews': () => ({ reviews: [finished, { ...finished, attempt_id: 'other', review_state: 'waiting' }] }),
    });
    const pending = deferred();
    page.handlers['/api/assessments'] = () => pending.promise;
    await page.tick(5000);
    await page.click('Review answers');
    pending.resolve({ assessments: [] });
    await page.flush();
    assert.equal(page.elements.get('status-title').textContent, 'First test');
    assert.ok(page.getButton('Back to assessments'));
    await page.click('Back to assessments');
    assert.equal(page.elements.get('status-title').textContent, 'Available assessments');
  },
  async attempt_poll_reuses_embedded_result() {
    const pending = deferred();
    const page = await boot('sealed_pending');
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => pending.promise;
    await page.tick(1000);
    await page.tick(1000);
    assert.equal(page.calls.filter(call => call.path === `/api/attempts/${finished.attempt_id}`).length, 2,
      'Attempt polling must not overlap a request already in flight');
    pending.resolve({ state: 'acknowledged_result', attempt_id: finished.attempt_id, result: finished });
    await page.flush();
    assert.equal(page.elements.get('status-title').textContent, 'Result received');
    assert.match(page.actions().textContent, /1 \/ 1 \(100%\)/);
    assert.equal(page.calls.some(call => call.path.endsWith('/result')), false);
  },
  async pending_start_keeps_navigation_from_hiding_the_attempt() {
    const pendingStart = deferred();
    const page = await boot('waiting_or_ready', {
      '/api/assessments': () => ({ assessments: [launch, { ...launch, release_id: 'third-release' }] }),
      '/api/assessments/second-release/start': () => pendingStart.promise,
    });
    await page.click('Start');
    assert.ok(page.actions().querySelectorAll('button').every(button => button.disabled),
      'A pending test start must disable other starts, reviews, refresh, and signout');
    pendingStart.resolve(attempt);
    await page.flush();
    assert.equal(page.elements.get('assessment-panel').hidden, false);
    assert.equal(page.elements.get('question-text').textContent, 'Second test question');
  },
  async pending_signout_keeps_navigation_from_reviving_the_session() {
    const pendingLogout = deferred();
    const page = await boot('acknowledged_result', { '/api/logout': () => pendingLogout.promise });
    await page.click('Sign out');
    assert.ok(page.actions().querySelectorAll('button').every(button => button.disabled),
      'The result cannot navigate away while signout is pending');
    pendingLogout.resolve({ state: 'login' });
    await page.flush();
    assert.equal(page.elements.get('status-title').textContent, 'Student sign in');
  },
  async result_fallback_remains_supported() {
    const page = await boot('sealed_pending');
    page.handlers[`/api/attempts/${finished.attempt_id}`] = () => ({
      state: 'acknowledged_result', attempt_id: finished.attempt_id,
    });
    page.handlers[`/api/attempts/${finished.attempt_id}/result`] = () => ({ result: finished });
    await page.tick(1000);
    assert.equal(page.elements.get('status-title').textContent, 'Result received');
    assert.match(page.actions().textContent, /1 \/ 1 \(100%\)/);
  },
};

scenarios[process.argv[3]]().then(() => process.stdout.write(JSON.stringify({ passed: true }))).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
