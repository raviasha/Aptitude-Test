const app = document.querySelector('#app');
const toast = document.querySelector('#toast');
let state = { user: null, attempt: null, questionIndex: 0 };

const categories = ['Quantitative Aptitude', 'Logical Reasoning', 'Data Interpretation', 'Verbal Ability', 'Coding / Computational Thinking'];
const short = value => ({'Quantitative Aptitude':'Quantitative','Logical Reasoning':'Logical','Data Interpretation':'Data','Verbal Ability':'Verbal','Coding / Computational Thinking':'Coding'}[value] || value);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const pct = value => `${Number(value || 0).toFixed(1).replace('.0','')}%`;
const optionEntries = question => Object.entries(question.options || {});
const compositionTotal = test => (test.selection_rules || []).reduce((sum, rule) => sum + Number(rule.quantity || 0), 0);

function taxonomyMarkup(taxonomy, prefix) {
  if (!taxonomy?.categories?.length) return '<p class="muted">This bank has no active questions.</p>';
  return taxonomy.categories.map(category => `<article class="taxonomy-category"><div><strong>${esc(category.name)}</strong><small>${category.count} available</small></div><div class="chapter-grid">${category.chapters.map(chapter => `<label><span>${esc(chapter.name)} <small>${chapter.count}</small></span><input type="number" min="0" max="${chapter.count}" value="0" data-rule-prefix="${esc(prefix)}" data-rule-category="${esc(category.name)}" data-rule-chapter="${esc(chapter.name)}" /></label>`).join('')}</div></article>`).join('');
}

function selectedRules(container) {
  return [...container.querySelectorAll('[data-rule-category]')].map(input => ({category:input.dataset.ruleCategory,chapter:input.dataset.ruleChapter,quantity:Number(input.value || 0)})).filter(rule => rule.quantity > 0);
}

function updateCompositionTotal(container, totalElement, maximum) {
  const total = selectedRules(container).reduce((sum, rule) => sum + rule.quantity, 0);
  totalElement.textContent = String(total); totalElement.classList.toggle('over-limit', total > maximum); return total;
}

async function attachTaxonomySelector(select, container, totalElement, maximum, prefix) {
  const render = async () => {
    totalElement.textContent = '0';
    if (!select.value) { container.innerHTML = '<p class="muted">Choose a question bank to see its categories and chapters.</p>'; return; }
    try {
      const taxonomy = await api(`/api/question-banks/${select.value}/taxonomy`);
      container.innerHTML = taxonomyMarkup(taxonomy, prefix);
      container.querySelectorAll('input').forEach(input => input.addEventListener('input', () => updateCompositionTotal(container, totalElement, maximum)));
    } catch (error) { container.innerHTML = `<p class="muted">${esc(error.message)}</p>`; }
  };
  select.addEventListener('change', render); if (select.value) await render();
}

function structuredChartMarkup(stimulus) {
  const content = stimulus.content || {}, labels = Array.isArray(content.labels) ? content.labels : [], series = Array.isArray(content.series) ? content.series : [];
  const values = series.flatMap(item => Array.isArray(item.values) ? item.values.map(Number) : []).filter(Number.isFinite);
  if (!labels.length || !series.length || !values.length) return '<p class="muted">Chart data is unavailable.</p>';
  const maximum = Math.max(...values, 1), colors = ['#5578ef','#16865f','#d68232','#9a5bd1'];
  if ((content.chart_type || content.kind) === 'line') {
    const left = 45, top = 20, chartWidth = 540, chartHeight = 220;
    const polylines = series.map((item, seriesIndex) => `<polyline points="${item.values.map((value,index) => `${left + chartWidth * index / Math.max(1, labels.length - 1)},${top + chartHeight - Number(value) / maximum * chartHeight}`).join(' ')}" fill="none" stroke="${colors[seriesIndex % colors.length]}" stroke-width="3" />`).join('');
    const labelNodes = labels.map((label,index) => `<text x="${left + chartWidth * index / Math.max(1, labels.length - 1)}" y="275" text-anchor="middle" font-size="11">${esc(label)}</text>`).join('');
    return `<svg viewBox="0 0 620 300" role="img" aria-label="${esc(stimulus.alt_text || stimulus.title || 'Line chart')}"><line x1="${left}" y1="${top + chartHeight}" x2="${left + chartWidth}" y2="${top + chartHeight}" stroke="#7b879b"/>${polylines}${labelNodes}</svg>`;
  }
  const bars = [], groupWidth = 520 / labels.length, barWidth = Math.max(8, (groupWidth - 12) / series.length);
  labels.forEach((label,labelIndex) => series.forEach((item,seriesIndex) => { const value = Number(item.values?.[labelIndex] || 0), height = value / maximum * 210; bars.push(`<rect x="${52 + labelIndex * groupWidth + seriesIndex * barWidth}" y="${235 - height}" width="${Math.max(5, barWidth - 3)}" height="${height}" fill="${colors[seriesIndex % colors.length]}"/><text x="${52 + labelIndex * groupWidth + groupWidth / 2}" y="260" text-anchor="middle" font-size="11">${esc(label)}</text>`); }));
  return `<svg viewBox="0 0 620 285" role="img" aria-label="${esc(stimulus.alt_text || stimulus.title || 'Bar chart')}"><line x1="45" y1="235" x2="590" y2="235" stroke="#7b879b"/>${bars.join('')}</svg>`;
}

function stimulusMarkup(stimulus) {
  if (!stimulus) return '';
  const caption = stimulus.title ? `<figcaption>${esc(stimulus.title)}</figcaption>` : '';
  if (stimulus.url) return `<figure class="stimulus">${caption}<img src="${esc(stimulus.url)}" alt="${esc(stimulus.alt_text || stimulus.title || 'Question graph')}" /></figure>`;
  if (stimulus.type === 'table') { const columns = Array.isArray(stimulus.content?.columns) ? stimulus.content.columns : [], rows = Array.isArray(stimulus.content?.rows) ? stimulus.content.rows : []; return `<figure class="stimulus">${caption}<table><thead><tr>${columns.map(column => `<th>${esc(column)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${esc(value)}</td>`).join('')}</tr>`).join('')}</tbody></table></figure>`; }
  return `<figure class="stimulus">${caption}${structuredChartMarkup(stimulus)}</figure>`;
}
const date = value => value ? new Intl.DateTimeFormat('en-IN', {day:'2-digit',month:'short',year:'numeric'}).format(new Date(value)) : '—';

async function api(path, options = {}) {
  const form = options.body instanceof FormData;
  const headers = {...(options.headers || {})};
  if (options.body && !form && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const config = {...options, headers};
  if (config.body && !form && typeof config.body !== 'string') config.body = JSON.stringify(config.body);
  const response = await fetch(path, config);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || 'Something went wrong.');
  }
  return response.headers.get('content-type')?.includes('application/json') ? response.json() : response;
}

function notify(message, bad = false) {
  toast.textContent = message; toast.className = bad ? 'bad show' : 'show';
  setTimeout(() => { toast.className = ''; }, 3500);
}

function logoutButton() { return `${state.user?.role === 'admin' ? '<button class="ghost" data-stop-server>Stop server</button>' : ''}<button class="ghost" data-logout>Sign out</button>`; }
function layout(title, subtitle, content, nav = '') {
  app.innerHTML = `<header class="top"><a class="brand" href="#" data-home><span>A</span>Aptitude <i>Lab</i></a><nav>${nav}</nav>${logoutButton()}</header><main><div class="heading"><div><p class="eyebrow">College LAN assessment server</p><h1>${title}</h1><p>${subtitle || ''}</p></div></div>${content}</main>`;
  document.querySelector('[data-logout]')?.addEventListener('click', async () => { await api('/api/logout', {method:'POST'}); state = {user:null,attempt:null,questionIndex:0}; loginScreen(); });
  document.querySelector('[data-stop-server]')?.addEventListener('click', async () => { if (!confirm('Stop the Aptitude Lab server? All connected users will be disconnected.')) return; await api('/api/admin/shutdown', {method:'POST'}); app.innerHTML = '<div class="login-card"><h2>Server stopped</h2><p>You can close this browser window.</p></div>'; });
  document.querySelector('[data-home]')?.addEventListener('click', event => { event.preventDefault(); home(); });
  document.querySelectorAll('[data-nav]').forEach(button => button.addEventListener('click', () => admin(button.dataset.nav)));
}

function loginScreen() {
  app.innerHTML = `<div class="login"><section><a class="brand"><span>A</span>Aptitude <i>Lab</i></a><p class="eyebrow">Local, secure assessment platform</p><h1>Know where<br/>you <em>stand.</em></h1><p>Randomized aptitude tests with server-side scoring and useful progress signals.</p><small>Runs inside your college network. No internet required.</small></section><section class="login-card"><p class="eyebrow">Welcome back</p><h2>Sign in</h2><div class="role"><button class="chosen" data-role="student">Student</button><button data-role="admin">Faculty</button></div><form id="login-form"><label id="login-id-label">Student ID / USN<input id="identifier" required placeholder="1KS23AI042" autocomplete="username" /></label><label>Password<input id="password" type="password" required placeholder="Enter password" autocomplete="current-password" /></label><button class="primary">Sign in →</button></form><p class="demo" id="demo">Student demo: <code>1KS23AI042</code> / <code>student123</code></p><button class="secondary link" id="register-link">New student? Register here</button></section></div>`;
  let role = 'student';
  document.querySelectorAll('[data-role]').forEach(button => button.addEventListener('click', () => {
    role = button.dataset.role; document.querySelectorAll('[data-role]').forEach(item => item.classList.toggle('chosen', item === button));
    document.querySelector('#login-id-label').firstChild.textContent = role === 'student' ? 'Student ID / USN' : 'Faculty username';
    document.querySelector('#identifier').placeholder = role === 'student' ? '1KS23AI042' : 'faculty';
    document.querySelector('#demo').innerHTML = role === 'student' ? 'Student demo: <code>1KS23AI042</code> / <code>student123</code>' : 'Faculty demo: <code>faculty</code> / <code>faculty123</code>';
  }));
  document.querySelector('#login-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { state.user = (await api('/api/login', {method:'POST', body:{identifier:document.querySelector('#identifier').value,password:document.querySelector('#password').value,role}})).user; home(); }
    catch (error) { notify(error.message, true); }
  });
  document.querySelector('#register-link').addEventListener('click', registerScreen);
}

function registerScreen() {
  app.innerHTML = `<div class="login"><section><a class="brand"><span>A</span>Aptitude <i>Lab</i></a><p class="eyebrow">Student registration</p><h1>Ready to<br/><em>begin?</em></h1><p>Create your own student account for this lab.</p><small>Use your official Student ID / USN.</small></section><section class="login-card"><p class="eyebrow">New student</p><h2>Create account</h2><form id="register-form"><label>Student name<input name="name" required autocomplete="name" /></label><label>Student ID / USN<input name="student_id" required /></label><div class="split"><label>Class<input name="student_class" value="AI & DS" required /></label><label>Section<input name="section" value="A" required /></label></div><label>Password<input name="password" type="password" minlength="6" required autocomplete="new-password" /></label><button class="primary">Create account →</button></form><button class="secondary link" id="back-login">Back to sign in</button></section></div>`;
  document.querySelector('#back-login').addEventListener('click', loginScreen);
  document.querySelector('#register-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await api('/api/register', {method:'POST', body:Object.fromEntries(new FormData(event.currentTarget))}); notify('Account created. You can now sign in.'); loginScreen(); }
    catch (error) { notify(error.message, true); }
  });
}

async function home() { return state.user?.role === 'admin' ? admin('overview') : studentDashboard(); }

async function studentDashboardLegacy() {
  const data = await api('/api/student/dashboard');
  const test = data.test, active = data.active_attempt;
  layout(`Good to see you, <em>${esc(data.student.name.split(' ')[0])}.</em>`, 'Choose an assessment, work at your pace, and see exactly where you can improve.', `
    <section class="hero"><div><p class="eyebrow">${data.launched ? 'Faculty-launched assessment' : 'Available assessments'}</p><h2>${test ? esc(test.test_name) : 'No assessment is available'}</h2><p>${test ? (data.launched ? 'Only this launched assessment can be attempted now.' : 'Choose an assessment to begin.') : 'Ask faculty to publish a test for your class.'}</p></div>${active ? `<button class="primary" data-resume="${active.attempt_id}">Resume assessment →</button>` : ''}</section>
    <section class="grid two">${data.tests.map(item => `<article class="card"><p class="eyebrow">${item.launched ? 'Launched now' : 'Available'}</p><h2>${esc(item.test_name)}</h2><button class="primary" data-start="${item.test_id}">Start assessment →</button></article>`).join('')}</section>
    <section class="grid two"><article class="card"><p class="eyebrow">Assessment history</p><h2>Your results</h2>${data.history.length ? `<table><thead><tr><th>Assessment</th><th>Date</th><th>Score</th></tr></thead><tbody>${data.history.map(item => `<tr><td>${esc(item.test_name)}</td><td>${date(item.submitted_at)}</td><td><button class="result-link" data-result="${item.attempt_id}">${item.score}/${item.total_questions} · ${pct(item.percentage)}</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">Your completed assessments will appear here.</p>'}</article>
    <article class="card"><p class="eyebrow">Skills map</p><h2>Category performance</h2>${data.category_trend.length ? data.category_trend.map(item => `<div class="bar"><div><span>${esc(short(item.category))}</span><b>${pct(item.percentage)}</b></div><i><em style="width:${item.percentage}%"></em></i></div>`).join('') : '<p class="muted">Complete an assessment to build your skills map.</p>'}</article></section>`);
  document.querySelectorAll('[data-start]').forEach(button => button.addEventListener('click', async () => { const result = await api(`/api/tests/${button.dataset.start}/start`, {method:'POST'}); loadAttempt(result.attempt_id); }));
  document.querySelectorAll('[data-resume]').forEach(button => button.addEventListener('click', () => loadAttempt(button.dataset.resume)));
  document.querySelectorAll('[data-result]').forEach(button => button.addEventListener('click', () => resultScreen(button.dataset.result)));
}

async function studentDashboard() {
  const [data, catalog] = await Promise.all([api('/api/student/dashboard'), api('/api/student/practice/catalog')]);
  const test = data.test, active = data.active_attempt, practiceLocked = data.launched;
  const heroSession = active || test;
  const heroIsPractice = active?.mode === 'student_practice';
  const heroEyebrow = active ? (heroIsPractice ? 'Personal practice in progress' : 'Faculty assessment in progress') : (data.launched ? 'Faculty-launched assessment' : 'Available assessments');
  const heroDescription = active
    ? `Continue ${heroIsPractice ? 'your saved practice set' : 'this saved assessment'}: ${active.test_name}.`
    : (test ? (data.launched ? 'Only this launched assessment can be attempted now.' : 'Choose an assessment below when you are ready.') : 'You can still build your own practice set below.');
  layout(`Good to see you, <em>${esc(data.student.name.split(' ')[0])}.</em>`, 'Build a focused practice set or continue a Faculty assessment.', `
    <section class="hero"><div><p class="eyebrow">${heroEyebrow}</p><h2>${heroSession ? esc(heroSession.test_name) : 'No Faculty assessment is available'}</h2><p>${esc(heroDescription)}</p></div>${active ? `<button class="primary" data-resume="${active.attempt_id}">Resume ${heroIsPractice ? 'practice' : 'assessment'} →</button>` : ''}</section>
    <section class="grid two"><article class="card practice-builder"><p class="eyebrow">Personal practice</p><h2>Build a practice set</h2><p class="muted">Choose exactly how many questions you want from each category and chapter.</p><form id="practice-form"><label>Question bank<select id="practice-bank" required><option value="">Choose a bank…</option>${catalog.banks.map(bank => `<option value="${bank.bank_id}">${esc(bank.bank_name)} · ${bank.question_count} questions</option>`).join('')}</select></label><div id="practice-composition" class="taxonomy"><p class="muted">Choose a question bank to see its categories and chapters.</p></div><p class="composition-total">Selected: <strong id="practice-total">0</strong> / ${catalog.maximum_questions}</p><button class="primary" ${practiceLocked ? 'disabled title="Practice is paused during a launched assessment."' : ''}>Start random practice →</button></form>${practiceLocked ? '<p class="muted">Personal practice is paused while Faculty has a live assessment.</p>' : ''}</article>
    <article class="card"><p class="eyebrow">Faculty assessments</p><h2>Available now</h2>${data.tests.length ? data.tests.map(item => `<article class="assessment-choice"><div><strong>${esc(item.test_name)}</strong><small>${item.launched ? 'Launched now' : 'Available'}</small></div><button class="primary small" data-start="${item.test_id}">Start →</button></article>`).join('') : '<p class="muted">No Faculty assessment is currently available.</p>'}</article></section>
    <section class="grid two"><article class="card"><p class="eyebrow">Practice and assessment history</p><h2>Your results</h2>${data.history.length ? `<table><thead><tr><th>Session</th><th>Date</th><th>Score</th></tr></thead><tbody>${data.history.map(item => `<tr><td>${esc(item.test_name)}<small>${item.mode === 'student_practice' ? 'Personal practice' : 'Faculty assessment'}</small></td><td>${date(item.submitted_at)}</td><td><button class="result-link" data-result="${item.attempt_id}">${item.score}/${item.total_questions} · ${pct(item.percentage)}</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">Completed sessions will appear here.</p>'}</article>
    <article class="card"><p class="eyebrow">Skills map</p><h2>Category performance</h2>${data.category_trend.length ? data.category_trend.map(item => `<div class="bar"><div><span>${esc(short(item.category))}</span><b>${pct(item.percentage)}</b></div><i><em style="width:${item.percentage}%"></em></i></div>`).join('') : '<p class="muted">Complete a session to build your skills map.</p>'}</article></section>`);
  const bankSelect = document.querySelector('#practice-bank'), composition = document.querySelector('#practice-composition'), total = document.querySelector('#practice-total');
  await attachTaxonomySelector(bankSelect, composition, total, catalog.maximum_questions, 'practice');
  document.querySelector('#practice-form').addEventListener('submit', async event => { event.preventDefault(); const selection_rules = selectedRules(composition); try { const result = await api('/api/student/practice/start', {method:'POST',body:{bank_id:Number(bankSelect.value),selection_rules}}); loadAttempt(result.attempt_id); } catch(error) { notify(error.message,true); } });
  document.querySelectorAll('[data-start]').forEach(button => button.addEventListener('click', async () => { try { const result = await api(`/api/tests/${button.dataset.start}/start`, {method:'POST'}); loadAttempt(result.attempt_id); } catch(error) { notify(error.message,true); } }));
  document.querySelectorAll('[data-resume]').forEach(button => button.addEventListener('click', () => loadAttempt(button.dataset.resume)));
  document.querySelectorAll('[data-result]').forEach(button => button.addEventListener('click', () => resultScreen(button.dataset.result).catch(error => notify(error.message,true))));
}

async function loadAttempt(id) { state.attempt = await api(`/api/attempts/${id}`); state.questionIndex = 0; renderAttempt(); }
function renderAttempt() {
  const attempt = state.attempt, q = attempt.questions[state.questionIndex], answered = attempt.questions.filter(item => item.selected_answer).length;
  const feedback = attempt.feedback_allowed && q.feedback ? `<article class="card feedback"><h3>${q.feedback.correct ? 'Correct' : 'Not quite'}</h3><p><strong>Correct answer:</strong> ${q.feedback.correct_answer}</p><p>${esc(q.feedback.explanation)}</p>${q.feedback.solution_steps?.length ? `<h4>Solution steps</h4><ol>${q.feedback.solution_steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : ''}<ul>${optionEntries(q).map(([key,value]) => `<li><strong>${key}:</strong> ${esc(value)} — ${esc(q.feedback.option_explanations?.[key] || (key === q.feedback.correct_answer ? 'Correct option.' : 'This option is not the correct answer.'))}</li>`).join('')}</ul></article>` : '';
  const questionContent = `${stimulusMarkup(q.stimulus)}${q.question_html ? `<div class="visual-question">${q.question_html}</div>` : `<h1>${esc(q.question_text)}</h1>`}`;
  app.innerHTML = `<header class="top"><a class="brand"><span>A</span>Aptitude <i>Lab</i></a><div class="save">✓ Answer saved automatically</div><button class="ghost" data-exit>Save and exit</button></header><main class="assessment"><aside><p class="eyebrow">Progress</p><strong>${answered} <small>/ ${attempt.questions.length}</small></strong><p>${answered} answered</p><div class="numbers">${attempt.questions.map((item,index) => `<button class="${index===state.questionIndex?'current':''} ${item.selected_answer?'done':''}" data-index="${index}">${index+1}</button>`).join('')}</div></aside><section class="question"><div class="question-meta"><span>${esc(short(q.category))} · ${esc(q.chapter)}</span><span>Question ${state.questionIndex+1} of ${attempt.questions.length}</span></div>${questionContent}<div class="answers">${optionEntries(q).map(([key,value]) => `<button class="${q.selected_answer===key?'selected':''}" data-answer="${key}" ${attempt.feedback_allowed && q.selected_answer ? 'disabled' : ''}><i>${key}</i>${esc(value)}<b>${q.selected_answer===key?'✓':''}</b></button>`).join('')}</div>${feedback}<footer><button class="secondary" data-prev ${state.questionIndex===0?'disabled':''}>← Previous</button>${state.questionIndex===attempt.questions.length-1 ? '<button class="primary" data-submit>Review & submit →</button>' : '<button class="primary" data-next>Next question →</button>'}</footer></section></main>`;
  document.querySelectorAll('[data-index]').forEach(button => button.addEventListener('click', () => { state.questionIndex = Number(button.dataset.index); renderAttempt(); }));
  document.querySelectorAll('[data-answer]').forEach(button => button.addEventListener('click', () => saveAnswer(q.question_id, button.dataset.answer)));
  document.querySelector('[data-prev]')?.addEventListener('click', () => { state.questionIndex--; renderAttempt(); });
  document.querySelector('[data-next]')?.addEventListener('click', () => { state.questionIndex++; renderAttempt(); });
  document.querySelector('[data-submit]')?.addEventListener('click', submitAttempt);
  document.querySelector('[data-exit]')?.addEventListener('click', studentDashboard);
}
async function saveAnswer(questionId, answer) { const result = await api(`/api/attempts/${state.attempt.attempt_id}/responses/${questionId}`, {method:'PUT',body:{answer}}); const question = state.attempt.questions.find(item => item.question_id === questionId); question.selected_answer = answer; if (result.feedback) question.feedback = result.feedback; renderAttempt(); }
async function submitAttempt() { const check = await api(`/api/attempts/${state.attempt.attempt_id}/submit`, {method:'POST',body:{confirmed:false}}); if (check.requires_confirmation && !confirm(`${check.unanswered} question(s) remain unanswered. Submit anyway?`)) return; if (check.feedback_allowed === false) { app.innerHTML = '<main class="result"><a class="brand"><span>A</span>Aptitude <i>Lab</i></a><p class="eyebrow">Assessment submitted</p><h1>Thank you</h1><p class="big">Your responses have been recorded. Faculty will release scores later.</p><button class="primary" data-dashboard>Back to dashboard →</button></main>'; document.querySelector('[data-dashboard]').addEventListener('click', studentDashboard); return; } resultScreen(state.attempt.attempt_id, check.requires_confirmation); }
async function resultScreenLegacy(id, confirmSubmit = false) { const data = confirmSubmit ? await api(`/api/attempts/${id}/submit`, {method:'POST',body:{confirmed:true}}) : await api(`/api/attempts/${id}/result`); const a = data.attempt; app.innerHTML = `<main class="result"><a class="brand"><span>A</span>Aptitude <i>Lab</i></a><p class="eyebrow">Assessment complete</p><h1>${a.score} / ${a.total_questions}</h1><p class="big">${pct(a.percentage)} overall score</p><div class="score-stats"><span><b>${a.correct}</b> Correct</span><span><b>${data.incorrect}</b> Incorrect</span><span><b>${data.unanswered}</b> Unanswered</span></div><article class="card"><p class="eyebrow">Category performance</p>${data.categories.map(item => `<div class="bar"><div><span>${esc(item.category)}</span><b>${item.correct}/${item.total} · ${pct(item.percentage)}</b></div><i><em style="width:${item.percentage}%"></em></i></div>`).join('')}</article><button class="primary" data-dashboard>Back to dashboard →</button></main>`; document.querySelector('[data-dashboard]').addEventListener('click', studentDashboard); }

async function resultScreen(id, confirmSubmit = false) {
  const data = confirmSubmit ? await api(`/api/attempts/${id}/submit`, {method:'POST',body:{confirmed:true}}) : await api(`/api/attempts/${id}/result`), a = data.attempt;
  app.innerHTML = `<main class="result"><a class="brand"><span>A</span>Aptitude <i>Lab</i></a><p class="eyebrow">${a.mode === 'student_practice' ? 'Practice complete' : 'Assessment complete'}</p><h1>${a.score} / ${a.total_questions}</h1><p class="big">${pct(a.percentage)} overall score</p><div class="score-stats"><span><b>${a.correct}</b> Correct</span><span><b>${data.incorrect}</b> Incorrect</span><span><b>${data.unanswered}</b> Unanswered</span></div><article class="card"><p class="eyebrow">Chapter performance</p>${data.chapters.map(item => `<div class="bar"><div><span>${esc(item.category)} · ${esc(item.chapter)}</span><b>${item.correct}/${item.total} · ${pct(item.percentage)}</b></div><i><em style="width:${item.percentage}%"></em></i></div>`).join('')}</article><p>${a.mode === 'student_practice' && data.incorrect ? '<button class="secondary" data-retry>Retry incorrect questions</button> ' : ''}<button class="primary" data-dashboard>Back to dashboard →</button></p></main>`;
  document.querySelector('[data-dashboard]').addEventListener('click', studentDashboard);
  document.querySelector('[data-retry]')?.addEventListener('click', async () => { try { const result = await api(`/api/student/practice/${id}/retry-incorrect`, {method:'POST'}); loadAttempt(result.attempt_id); } catch(error) { notify(error.message,true); } });
}

function adminNav(active) { return `<button class="${active==='overview'?'active':''}" data-nav="overview">Overview</button><button class="${active==='banks'?'active':''}" data-nav="banks">Question banks</button><button class="${active==='tests'?'active':''}" data-nav="tests">Tests</button><button class="${active==='students'?'active':''}" data-nav="students">Students</button>`; }
async function admin(view) { if (view === 'banks') return questionBanks(); if (view === 'tests') return tests(); if (view === 'students') return students(); const data = await api('/api/admin/dashboard'); layout('Class performance, <em>at a glance.</em>', 'Live summary of submitted assessments across the lab.', `<section class="metrics"><article><span>Enrolled students</span><b>${data.totals.students}</b></article><article><span>Tests completed</span><b>${data.totals.completed}</b></article><article class="dark"><span>Class average</span><b>${pct(data.totals.average)}</b></article></section><section class="grid two"><article class="card"><p class="eyebrow">Class analytics</p><h2>Category performance</h2>${data.category_performance.length ? data.category_performance.map(item => `<div class="bar"><div><span>${esc(item.category)}</span><b>${pct(item.percentage)}</b></div><i><em style="width:${item.percentage}%"></em></i></div>`).join('') : '<p class="muted">Data will appear once students complete tests.</p>'}</article><article class="card"><p class="eyebrow">Exports and backup</p><h2>Keep records safe</h2><p class="muted">Download results for Excel or a full SQLite backup for safe storage.</p><p><a class="secondary link" href="/api/admin/export">Download results CSV</a> <a class="secondary link" href="/api/admin/backup">Download database backup</a></p></article></section>`, adminNav('overview')); }

async function questionBanks() {
  const [library, staged, folder] = await Promise.all([api('/api/admin/question-banks'),api('/api/admin/question-banks/staged'),api('/api/admin/question-banks/folder')]);
  layout('Question <em>banks.</em>', 'Every bank is an HTML visual file plus a private answer-key JSON file.', `<section class="folder-card"><div><p class="eyebrow">Manual copy-paste folder</p><h2>Drop pairs here, then import</h2><code>${esc(folder.path)}</code><p>Files must share a base name, for example <code>placement-set-02.html</code> and <code>placement-set-02.json</code>.</p></div><button class="secondary" data-refresh>Refresh folder</button></section><section class="grid two"><article class="card"><p class="eyebrow">Files detected in folder</p><h2>Ready to import</h2>${staged.pairs.length ? `<table><thead><tr><th>Pair</th><th>Status</th><th></th></tr></thead><tbody>${staged.pairs.map(item => `<tr><td><strong>${esc(item.stem)}</strong><small>${esc(item.html_filename || 'HTML missing')}<br/>${esc(item.answer_key_filename || 'JSON missing')}</small></td><td>${item.ready ? '<span class="ok">Ready</span>' : '<span class="warn">Incomplete</span>'}</td><td>${item.ready ? `<button class="primary small" data-import-folder data-html="${esc(item.html_filename)}" data-json="${esc(item.answer_key_filename)}">Import</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No HTML/JSON pairs found in the folder yet.</p>'}</article><article class="card"><p class="eyebrow">Alternative</p><h2>Upload a pair</h2><p class="muted">Use this from any faculty computer if the files are not already on the server.</p><form id="upload-bank"><label>Questions and visuals (HTML)<input name="html_file" type="file" accept=".html,.htm" required /></label><label>Choices and answer key (JSON)<input name="answer_key_file" type="file" accept=".json" required /></label><button class="primary">Upload & import →</button></form></article></section><section class="card"><p class="eyebrow">Imported library</p><h2>Available question banks</h2><table><thead><tr><th>Bank</th><th>Questions</th><th>Format</th><th>Stimuli</th><th>Imported</th><th></th></tr></thead><tbody>${library.banks.map(item => `<tr><td><strong>${esc(item.bank_name)}</strong><small>${esc(item.source_html_filename)} + ${esc(item.answer_key_filename)}</small></td><td>${item.question_count}</td><td>v${item.format_version || 1}</td><td>${item.stimulus_count || 0}</td><td>${date(item.imported_at)}</td><td>${item.test_count ? `<small>Used by ${item.test_count} test(s)</small>` : ''}<button class="secondary small" data-delete-bank="${item.bank_id}" data-bank-name="${esc(item.bank_name)}" data-test-count="${item.test_count || 0}">Delete</button></td></tr>`).join('')}</tbody></table></section>`, adminNav('banks'));
  document.querySelector('.heading>div>p:last-child').textContent = 'Import a legacy HTML/JSON pair or a v2 ZIP with chapter data and reusable graph assets.';
  document.querySelector('#upload-bank').insertAdjacentHTML('afterend', `<hr/><p class="eyebrow">Recommended v2 format</p><h2>Upload a package</h2><p class="muted">One ZIP can contain chapter-based JSONL question files and reusable graph assets.</p><form id="upload-package"><label>Question-bank package<input name="package_file" type="file" accept=".zip" required /></label><button class="primary">Upload v2 package →</button></form>`);
  document.querySelector('[data-refresh]').addEventListener('click', questionBanks);
  document.querySelectorAll('[data-import-folder]').forEach(button => button.addEventListener('click', async () => { try { const result = await api('/api/admin/question-banks/import-from-folder',{method:'POST',body:{html_filename:button.dataset.html,answer_key_filename:button.dataset.json}}); notify(`${result.bank_name}: ${result.question_count} question(s) imported.`); questionBanks(); } catch(error) { notify(error.message,true); } }));
  document.querySelector('#upload-bank').addEventListener('submit', async event => { event.preventDefault(); try { const result = await api('/api/admin/question-banks/import',{method:'POST',body:new FormData(event.currentTarget)}); notify(`${result.bank_name}: ${result.question_count} question(s) imported.`); questionBanks(); } catch(error) { notify(error.message,true); } });
  document.querySelector('#upload-package').addEventListener('submit', async event => { event.preventDefault(); try { const result = await api('/api/admin/question-banks/import-package',{method:'POST',body:new FormData(event.currentTarget)}); notify(`${result.bank_name}: ${result.question_count} question(s), ${result.stimulus_count} stimulus item(s) imported.`); questionBanks(); } catch(error) { notify(error.message,true); } });
  document.querySelectorAll('[data-delete-bank]').forEach(button => button.addEventListener('click', async () => {
    const testCount = Number(button.dataset.testCount || 0);
    const dependentWarning = testCount ? ` This will also permanently delete ${testCount} dependent test(s), every attempt and response, and all result history.` : '';
    if (!confirm(`Delete “${button.dataset.bankName}” and its questions/visuals?${dependentWarning} This cannot be undone.`)) return;
    try {
      const result = await api(`/api/admin/question-banks/${button.dataset.deleteBank}`, {method:'DELETE'});
      const counts = result.deleted_counts || {};
      const historySummary = counts.tests ? `, ${counts.tests} dependent test(s), and ${counts.attempts || 0} attempt(s)` : '';
      notify(`${result.bank_name}${historySummary} deleted.`);
      questionBanks();
    } catch(error) { notify(error.message,true); }
  }));
}

async function testsLegacy() { const data = await api('/api/admin/tests'); layout('Create a <em>test.</em>', 'Launch one test for an exclusive exam, or leave all tests available for student choice.', `<section class="grid two"><article class="card"><p class="eyebrow">New assessment</p><h2>Question composition</h2><form id="test-form"><label>Test name<input name="test_name" required placeholder="Placement Readiness · Set 02" /></label><label>Question bank<select name="bank_id" required><option value="">Choose a bank…</option>${data.banks.map(bank => `<option value="${bank.bank_id}">${esc(bank.bank_name)} · ${bank.question_count} active questions</option>`).join('')}</select></label><div class="composition">${categories.map(category => `<label><span>${esc(category)}</span><input type="number" name="${esc(category)}" min="0" max="100" value="6" /></label>`).join('')}</div><button class="primary">Create test →</button></form></article><article class="card"><p class="eyebrow">Current tests</p><h2>Test library</h2><table><thead><tr><th>Name</th><th>Bank</th><th>Questions</th><th>Action</th></tr></thead><tbody>${data.tests.map(test => `<tr><td>${esc(test.test_name)}</td><td>${esc(test.bank_name || '—')}</td><td>${Object.values(test.composition).reduce((sum,value)=>sum+value,0)}</td><td>${test.launched ? '<button class="secondary small" data-close-test="'+test.test_id+'">Close</button>' : '<button class="primary small" data-launch-test="'+test.test_id+'">Launch</button>'}</td></tr>`).join('')}</tbody></table></article></section>`, adminNav('tests')); document.querySelector('#test-form').addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget)); const composition = {}; categories.forEach(category => composition[category] = Number(values[category] || 0)); try { await api('/api/admin/tests',{method:'POST',body:{test_name:values.test_name,bank_id:Number(values.bank_id),composition}}); notify('Test created.'); tests(); } catch(error) { notify(error.message,true); } }); document.querySelectorAll('[data-launch-test]').forEach(button => button.addEventListener('click', async () => { await api(`/api/admin/tests/${button.dataset.launchTest}/launch`, {method:'POST'}); notify('Test launched.'); tests(); })); document.querySelectorAll('[data-close-test]').forEach(button => button.addEventListener('click', async () => { await api(`/api/admin/tests/${button.dataset.closeTest}/close`, {method:'POST'}); notify('Test closed. Students can choose available tests.'); tests(); })); }

async function tests() {
  const data = await api('/api/admin/tests');
  layout('Create a <em>test.</em>', 'Choose quantities from the selected bank’s actual categories and chapters.', `<section class="grid two"><article class="card"><p class="eyebrow">New assessment</p><h2>Question composition</h2><form id="test-form"><label>Test name<input name="test_name" required placeholder="Placement Readiness · Set 02" /></label><label>Question bank<select id="test-bank" name="bank_id" required><option value="">Choose a bank…</option>${data.banks.map(bank => `<option value="${bank.bank_id}">${esc(bank.bank_name)} · ${bank.question_count} active questions</option>`).join('')}</select></label><div id="test-composition" class="taxonomy"><p class="muted">Choose a question bank to see its categories and chapters.</p></div><p class="composition-total">Selected: <strong id="test-total">0</strong> / 500</p><button class="primary">Create test →</button></form></article><article class="card"><p class="eyebrow">Current tests</p><h2>Test library</h2><table><thead><tr><th>Name</th><th>Bank</th><th>Questions</th><th>Action</th></tr></thead><tbody>${data.tests.map(test => `<tr><td>${esc(test.test_name)}</td><td>${esc(test.bank_name || '—')}</td><td>${compositionTotal(test)}</td><td>${test.launched ? `<button class="secondary small" data-close-test="${test.test_id}">Close</button>` : `<button class="primary small" data-launch-test="${test.test_id}">Launch</button>`}</td></tr>`).join('')}</tbody></table></article></section>`, adminNav('tests'));
  const bankSelect = document.querySelector('#test-bank'), composition = document.querySelector('#test-composition'), total = document.querySelector('#test-total');
  await attachTaxonomySelector(bankSelect, composition, total, 500, 'test');
  document.querySelector('#test-form').addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget)); try { await api('/api/admin/tests',{method:'POST',body:{test_name:values.test_name,bank_id:Number(bankSelect.value),selection_rules:selectedRules(composition)}}); notify('Test created.'); tests(); } catch(error) { notify(error.message,true); } });
  document.querySelectorAll('[data-launch-test]').forEach(button => button.addEventListener('click', async () => { try { await api(`/api/admin/tests/${button.dataset.launchTest}/launch`, {method:'POST'}); notify('Test launched.'); tests(); } catch(error) { notify(error.message,true); } }));
  document.querySelectorAll('[data-close-test]').forEach(button => button.addEventListener('click', async () => { await api(`/api/admin/tests/${button.dataset.closeTest}/close`, {method:'POST'}); notify('Test closed. Students can choose available tests.'); tests(); }));
}

async function students() { const data = await api('/api/admin/students'); layout('Manage <em>students.</em>', 'Select one or more students to permanently remove their accounts and practice records.', `<section class="card"><div class="heading"><div><p class="eyebrow">Enrolled students</p><h2>${data.students.length} students</h2></div><button class="primary" id="delete-selected">Delete selected</button></div><table><thead><tr><th><input type="checkbox" id="select-all-students" /></th><th>Name</th><th>USN</th><th>Class</th></tr></thead><tbody>${data.students.map(student => `<tr><td><input type="checkbox" class="student-choice" value="${esc(student.student_id)}" /></td><td>${esc(student.name)}</td><td><code>${esc(student.student_id)}</code></td><td>${esc(student.class)}-${esc(student.section)}</td></tr>`).join('')}</tbody></table></section>`, adminNav('students')); document.querySelector('#select-all-students').addEventListener('change', event => document.querySelectorAll('.student-choice').forEach(choice => { choice.checked = event.target.checked; })); document.querySelector('#delete-selected').addEventListener('click', async () => { const selected = [...document.querySelectorAll('.student-choice:checked')].map(choice => choice.value); if (!selected.length) { notify('Select at least one student.', true); return; } if (!confirm(`Delete ${selected.length} selected student(s) and all practice records? This cannot be undone.`)) return; try { for (const studentId of selected) await api(`/api/admin/students/${encodeURIComponent(studentId)}`, {method:'DELETE'}); notify('Selected students deleted.'); students(); } catch(error) { notify(error.message,true); } }); }

async function boot() { try { state.user = (await api('/api/me')).user; state.user ? home() : loginScreen(); } catch { loginScreen(); } }
boot();
