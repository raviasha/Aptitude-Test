// Run with Playwright available (or NODE_PATH pointing to it):
// node tests/faculty_ui_browser.cjs [screenshot-directory]
// Uses disposable UI fixtures; never connects to a coordinator or student database.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require('playwright');

async function run() {
  const root = path.resolve(__dirname, '..');
  const output = process.argv[2] || await fs.mkdtemp(path.join(os.tmpdir(), 'ksat-faculty-ui-'));
  await fs.mkdir(output, {recursive: true});
  const server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const filename = pathname === '/' ? 'static/index.html' : pathname.slice(1);
    const target = path.resolve(root, filename);
    if (!target.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
    try {
      const content = await fs.readFile(target);
      response.setHeader('Content-Type', ({'.js':'text/javascript','.css':'text/css','.html':'text/html','.png':'image/png','.jpg':'image/jpeg'})[path.extname(target)] || 'application/octet-stream');
      response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({channel: process.env.KSAT_BROWSER_CHANNEL || 'msedge', headless: true});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, deviceScaleFactor: 1});
    const errors = [];
    const calls = [];
    page.on('pageerror', error => errors.push(error.message));
    const names = ['Number systems', 'HCF & LCM', 'Decimal fractions', 'Simplification', 'Square roots & cube roots', 'Average'];
    let banks = names.map((name, i) => ({bank_id: i + 1, bank_name: name, question_count: 70 + i * 19, test_count: i % 3, imported_at: '2026-09-10T09:30:00Z'}));
    const tests = ['Placement readiness · Set 01', 'Quantitative aptitude · Practice'].map((test_name, i) => ({test_id:i+1, test_name, bank_name:names[i], attempt_count:12, selection_rules:[{quantity:30}], release_state:'launched', launched:true, release_used:true, difficulty_levels:['Easy','Medium','Hard'], distributed_status:{started:12,submitted:7,voided:0}, remaining_seconds:420, exam_timing:{duration_seconds:1800,extension_seconds:0,active_count:5,earliest_remaining_seconds:1300,latest_remaining_seconds:1420}}));
    let holdUpload;
    let resolveUpload;
    let uploadFails = true;
    let books = [{book_id:1,title:'Quantitative aptitude'},{book_id:2,title:'Reasoning'}];
    let releasePreview;
    let delayPreview = false;
    let conflictCreate = true;
    const chapters = names.slice(0,3).map((chapter,i) => ({bank_id:i+1,chapter,bank_name:chapter,
      book_id:i===2?2:1,book_title:i===2?'Reasoning':'Quantitative aptitude',
      question_count:100,difficulties:{Easy:100}}));
    await page.route('**/api/**', async route => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      calls.push({path:pathname, method:request.method(), body:request.postData()});
      let data;
      if (pathname === '/api/me') data = {user:{role:'admin',name:'Faculty'},csrf_token:'disposable-ui-token'};
      else if (pathname === '/api/admin/dashboard') data = {totals:{students:128,completed:96,average:74.8}, category_performance:[{category:'Quantitative Aptitude',percentage:78},{category:'Logical Reasoning',percentage:72},{category:'Data Interpretation',percentage:68}],recent_attempts:[{name:'Demo student',student_id:'DEMO-001',test_name:'Placement readiness · Set 01',submitted_at:'2026-09-11T09:40:00Z',score:24,total_questions:30,percentage:80,violation_count:0}]};
      else if (pathname === '/api/admin/question-banks') data = {banks};
      else if (pathname === '/api/admin/books') data = {books};
      else if (pathname === '/api/admin/books/assign') {
        const body=request.postDataJSON();
        const book=body.book_id ? books.find(b=>b.book_id===body.book_id) : {book_id:3,title:body.book_title};
        if (!books.some(b=>b.book_id===book.book_id)) books.push(book);
        banks=banks.map(b=>body.bank_ids.includes(b.bank_id)?{...b,book_id:book.book_id,book_title:book.title}:b);
        data={...book,bank_ids:body.bank_ids};
      }
      else if (/^\/api\/admin\/books\/\d+$/.test(pathname)) {
        const book=books.find(b=>b.book_id===Number(pathname.split('/').pop()));
        book.title=request.postDataJSON().title; data=book;
      }
      else if (pathname === '/api/admin/chapter-catalogue') data = {chapters};
      else if (/^\/api\/question-banks\/\d+\/taxonomy$/.test(pathname)) data={
        bank_id:Number(pathname.split('/')[3]),bank_name:'Number systems',question_count:100,
        categories:[{name:'Quantitative Aptitude',question_count:100,chapters:[{name:'Number systems',
          question_count:100,difficulties:{Easy:100}}]}]};
      else if (pathname === '/api/admin/tests/preview') {
        const body=request.postDataJSON();
        const selected=chapters.filter(c=>body.chapters.some(s=>s.bank_id===c.bank_id && s.chapter===c.chapter));
        if ((body.difficulties.length===1 && body.difficulties[0]==='Hard') || body.total_questions<selected.length) {
          await route.fulfill({status:400,json:{detail:'No eligible questions for the selected difficulty.'}}); return;
        }
        const counts=selected.map(()=>Math.floor(body.total_questions/selected.length));
        counts.forEach((_,i)=>{if(i<body.total_questions%selected.length)counts[i]++;});
        data={total_questions:body.total_questions,preview_token:'fixture-'+body.total_questions,redistributed:false,
          allocations:selected.map((c,i)=>({...c,available:100,quantity:counts[i]}))};
        if (delayPreview) { delayPreview=false; await new Promise(resolve=>{releasePreview=resolve;}); }
      }
      else if (pathname === '/api/admin/tests' && request.method()==='POST') {
        if(conflictCreate){conflictCreate=false;await route.fulfill({status:409,json:{detail:'Refresh the chapter preview.'}});return;}
        data={created:true,test_id:9};
      }
        else if (pathname === '/api/admin/question-banks/import-package') {
          if (holdUpload) await holdUpload;
          if (request.postData().includes('filename="master.zip"')) {
            await route.fulfill({json:{results:[
              {filename:'chapters/01.zip',ok:true,message:'First chapter: 20 questions imported.'},
              {filename:'chapters/02.zip',ok:false,message:'Duplicate bank.'}
            ]}}); return;
          }
        if (uploadFails || request.postData().includes('filename="broken.zip"')) { await route.fulfill({status:400,json:{detail:'The ZIP does not contain a valid question-bank manifest.'}}); return; }
        banks = [...banks, {bank_id:99,bank_name:'New algebra bank',question_count:20,test_count:0,imported_at:'2026-09-11T10:00:00Z'}];
        data = banks[banks.length-1];
      }
      else if (pathname === '/api/admin/tests') data = {banks,tests,submission_queue_pending:0};
      else if (pathname === '/api/admin/devices') data = {devices:[]};
      else if (pathname === '/api/admin/students') data = {students:[{student_id:'DEMO-001',name:'Demo student',class:'AIML',section:'A',signed_in:false},{student_id:'DEMO-002',name:'Sample student',class:'CSE',section:'B',signed_in:true}]};
      else if (pathname === '/api/logout') data = {};
      else { errors.push('Unexpected API: '+pathname); await route.fulfill({status:404,json:{detail:'Unexpected fixture API'}}); return; }
      await route.fulfill({json:data});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByRole('heading', {name:'Overview',exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'overview-desktop.png'), fullPage:true});
    const nav = name => page.getByRole('navigation', {name:'Faculty navigation'}).getByRole('button', {name,exact:true});
    await nav('Question banks').click();
    await page.getByRole('heading', {name:'Question banks',exact:true}).waitFor();
    assert.equal(await page.locator('input[type=file]').count(), 1);
    assert.equal(await page.getByText('Upload a pair', {exact:true}).count(), 0);
    assert.equal(calls.some(call => /staged|folder|import-from-folder/.test(call.path)), false);
    assert.equal(await page.getByRole('button', {name:'Import question bank'}).isDisabled(), true);
    await page.screenshot({path:path.join(output,'question-banks-desktop.png'), fullPage:true});
    await page.getByRole('searchbox', {name:'Search question banks'}).fill('hcf');
    assert.equal(await page.locator('[data-bank-row]:visible').count(), 1);
    await page.getByRole('searchbox').fill('unmatched');
    assert.equal(await page.locator('#bank-empty').isVisible(), true);
    await page.getByRole('searchbox').fill('');
    const file = page.getByLabel('Question-bank ZIP');
    await file.setInputFiles({name:'answers.json',mimeType:'application/json',buffer:Buffer.from('{}')});
    assert.equal(await page.getByRole('button', {name:'Import question bank'}).isDisabled(), true);
    assert.match(await page.locator('#upload-status').innerText(), /choose a .zip/);
    // Exercise the drop target, not just the browser file picker.
    await page.locator('#zip-dropzone').evaluate(element => {
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(new File(['fixture'], 'chapter.ZIP', {type:'application/zip'}));
      element.dispatchEvent(new DragEvent('drop', {bubbles:true,cancelable:true,dataTransfer}));
    });
    assert.equal(await file.evaluate(element => element.files[0].name), 'chapter.ZIP');
    holdUpload = new Promise(resolve => { resolveUpload = resolve; });
    await page.getByRole('button', {name:'Import question bank'}).click();
    await page.getByRole('button', {name:'Importing…'}).waitFor();
    assert.equal(await file.isDisabled(), true);
    assert.equal(await page.getByRole('button', {name:'Importing…'}).isDisabled(), true);
    assert.equal(calls.filter(call => call.path.endsWith('/import-package')).length, 1);
    resolveUpload();
    await page.getByRole('button', {name:'Try import again'}).waitFor();
    assert.match(await page.locator('#upload-status').innerText(), /valid question-bank manifest/);
    assert.equal(await file.isDisabled(), false);
    uploadFails = false; holdUpload = null;
    await page.getByRole('button', {name:'Try import again'}).click();
    await page.getByText('New algebra bank', {exact:true}).waitFor();
    assert.match(calls.find(call => call.path.endsWith('/import-package')).body, /name="package_file"; filename="chapter.ZIP"/);
    const importCount = calls.filter(call => call.path.endsWith('/import-package')).length;
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByRole('button', {name:'Delete New algebra bank',exact:true}).click();
    assert.equal(calls.some(call => call.method === 'DELETE'), false);
    await nav('Tests').click();
    await page.getByRole('heading', {name:'Tests',exact:true}).waitFor();
    assert.equal(await page.locator('[data-close-test]').count(), 2);
    await page.screenshot({path:path.join(output,'tests-desktop.png'), fullPage:true});
    await page.getByText('Create an assessment', {exact:true}).click();
    assert.equal(await page.locator('#test-form').isVisible(), true);
    assert.equal(await page.getByLabel('Question selection').inputValue(),'balanced');
    await page.getByLabel('Test name',{exact:true}).fill('Across books');
    await page.getByLabel('Total questions',{exact:true}).fill('50');
    for (const chapter of names.slice(0,3)) await page.getByRole('checkbox',{name:chapter+' — '+chapter,exact:true}).check();
    await page.waitForFunction(()=>document.querySelector('#chapter-preview').textContent.includes('50 questions'));
    assert.deepEqual(await page.locator('[data-allocation-count]').allTextContents(),['17','17','16']);
    await page.locator('.chapter-book > summary').first().click();
    assert.equal(await page.locator('[data-chapter-key]').first().isChecked(),true);
    await page.locator('.chapter-book > summary').first().click();
    delayPreview=true;
    await page.getByLabel('Total questions',{exact:true}).fill('49');
    await page.waitForFunction(()=>document.querySelector('#create-test').disabled);
    while(!releasePreview) await new Promise(resolve=>setTimeout(resolve,10));
    await page.getByLabel('Total questions',{exact:true}).fill('48');
    await page.waitForFunction(()=>document.querySelector('#chapter-preview').textContent.includes('48 questions'));
    releasePreview();
    await page.waitForResponse(r=>r.url().endsWith('/tests/preview') && r.request().postDataJSON().total_questions===49);
    assert.deepEqual(await page.locator('[data-allocation-count]').allTextContents(),['16','16','16']);
    await page.locator('#test-difficulty').selectOption('Hard');
    await page.getByText('No eligible questions for the selected difficulty.',{exact:true}).waitFor();
    assert.equal(await page.locator('#create-test').isDisabled(),true);
    await page.locator('#test-difficulty').selectOption('Easy');
    await page.waitForFunction(()=>!document.querySelector('#create-test').disabled);
    await page.locator('#create-test').click();
    await page.getByRole('button',{name:'Refresh allocation'}).waitFor();
    assert.equal(await page.getByLabel('Test name',{exact:true}).inputValue(),'Across books');
    await page.getByRole('button',{name:'Refresh allocation'}).click();
    await page.waitForFunction(()=>!document.querySelector('#create-test').disabled);
    await page.waitForFunction(()=>!document.querySelector('#toast').classList.contains('show'));
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(output,'balanced-chapters-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'balanced-chapters-mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('#create-test').click();
    await page.waitForFunction(()=>!document.querySelector('.test-builder').open);
    const createdCall=calls.filter(c=>c.path==='/api/admin/tests'&&c.method==='POST').at(-1);
    assert.equal(JSON.parse(createdCall.body).chapters.length,3);
    assert.equal(JSON.parse(createdCall.body).bank_id,undefined);
    await page.getByText('Create an assessment',{exact:true}).click();
    await page.getByLabel('Question selection').selectOption('manual');
    await page.getByLabel('Test name',{exact:true}).fill('Manual regression');
    await page.locator('#test-bank').selectOption('1');
    await page.locator('[data-rule-category]').first().fill('3');
    await page.locator('#create-test').click();
    await page.waitForFunction(()=>!document.querySelector('.test-builder').open);
    const manual=JSON.parse(calls.filter(c=>c.path==='/api/admin/tests'&&c.method==='POST').at(-1).body);
    assert.equal(manual.bank_id,1);
    assert.equal(manual.selection_rules[0].quantity,3);
    await nav('Question banks').click();
    await page.getByRole('checkbox',{name:'Assign Number systems',exact:true}).check();
    await page.getByRole('checkbox',{name:'Assign HCF & LCM',exact:true}).check();
    await page.getByLabel('New book title',{exact:true}).fill('New textbook');
    await page.getByRole('button',{name:'Assign selected to book'}).click();
    await page.getByRole('option',{name:'New textbook',exact:true}).waitFor({state:'attached'});
    assert.equal(calls.filter(c=>c.path.endsWith('/import-package')).length,importCount);
    await page.locator('#assign-book').selectOption('3');
    page.once('dialog',dialog=>dialog.accept('Renamed textbook'));
    await page.getByRole('button',{name:'Rename selected book'}).click();
    await page.getByRole('option',{name:'Renamed textbook',exact:true}).waitFor({state:'attached'});
    // A background import must not take faculty away from their current page.
    await file.setInputFiles({name:'another.zip',mimeType:'application/zip',buffer:Buffer.from('fixture')});
    holdUpload = new Promise(resolve => { resolveUpload = resolve; });
    await page.getByRole('button', {name:'Import question bank'}).click();
    await page.getByRole('button', {name:'Importing…'}).waitFor();
    await nav('Overview').click();
    await page.getByRole('heading', {name:'Overview',exact:true}).waitFor();
    resolveUpload();
    await page.waitForResponse(response => response.url().endsWith('/import-package'));
    assert.equal(await page.getByRole('heading', {name:'Overview',exact:true}).isVisible(), true);
    await nav('Students').click();
    await page.getByRole('heading', {name:'Students',exact:true}).waitFor();
    assert.equal(await page.getByRole('button', {name:'Delete selected',exact:true}).isDisabled(), true);
    await page.getByRole('checkbox', {name:'Select Demo student',exact:true}).check();
    assert.equal(await page.getByRole('button', {name:'Delete selected (1)'}).isEnabled(), true);
    assert.equal(await page.getByRole('checkbox', {name:'Select all students'}).evaluate(el => el.indeterminate), true);
    await page.getByRole('checkbox', {name:'Select all students'}).check();
    assert.equal(await page.getByRole('button', {name:'Delete selected (2)'}).isEnabled(), true);
    await page.screenshot({path:path.join(output,'students-desktop.png'), fullPage:true});
    await page.setViewportSize({width:390,height:844});
    for (const name of ['Overview','Question banks','Tests','Students']) {
      await nav(name).click();
      await page.getByRole('heading', {name,exact:true}).waitFor();
      await page.screenshot({path:path.join(output,name.toLowerCase().replaceAll(' ','-')+'-mobile.png'),fullPage:true});
      const overflow = await page.evaluate(() => ({width:window.innerWidth, scroll:document.documentElement.scrollWidth, elements:[...document.querySelectorAll('main *')].filter(el => el.getBoundingClientRect().right > window.innerWidth && !el.closest('.table-scroll')).slice(0,12).map(el => ({tag:el.tagName,cls:el.className,right:el.getBoundingClientRect().right}))}));
      assert.equal(overflow.scroll <= overflow.width, true, `${name}: horizontal page overflow ${JSON.stringify(overflow)}`);
    }
    banks = [];
    await nav('Question banks').click();
    await page.getByRole('heading', {name:'Your library starts here'}).waitFor();
    assert.equal(await file.count(),1);
    assert.equal(calls.filter(call => call.path.endsWith('/import-package')).length, importCount + 1);
    holdUpload = null;
    assert.equal(await file.evaluate(element => element.multiple),true);
    await page.locator('#zip-dropzone').evaluate(element => {
      const dataTransfer = new DataTransfer();
      for (const name of ['first.zip','broken.zip','last.zip']) dataTransfer.items.add(new File(['fixture'],name,{type:'application/zip'}));
      element.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer}));
    });
    await page.getByRole('checkbox', {name:'Update an existing unused bank with the same name'}).check();
    await page.getByRole('button', {name:'Import question bank'}).click();
    await page.getByRole('heading', {name:'Import results: 2 succeeded, 1 failed'}).waitFor();
    const batchCalls = calls.filter(call => call.path.endsWith('/import-package')).slice(-3);
    assert.deepEqual(batchCalls.map(call => /filename="([^"]+)"/.exec(call.body)[1]),['first.zip','broken.zip','last.zip']);
    assert.ok(batchCalls.every(call => /name="replace_existing"\r\n\r\ntrue/.test(call.body)));
    assert.match(await page.locator('#bank-import-results').innerText(),/broken.zip.*Failed/);
    assert.equal(await file.evaluate(element => element.files.length),0,'successful files must not be left selected for retry');
      await page.screenshot({path:path.join(output,'bulk-import-results.png'),fullPage:true});
      await file.setInputFiles({name:'master.zip',mimeType:'application/zip',buffer:Buffer.from('fixture')});
      await page.getByRole('button', {name:'Import question bank'}).click();
      await page.getByRole('heading', {name:'Import results: 1 succeeded, 1 failed'}).waitFor();
      assert.match(await page.locator('#bank-import-results').innerText(), /master.zip.*chapters\/02.zip.*Duplicate bank/);
      assert.match(await page.locator('#toast').innerText(), /1 chapter\(s\) imported; 1 failed/);
      await page.screenshot({path:path.join(output,'master-import-results.png'),fullPage:true});
    await page.getByRole('button', {name:'Sign out',exact:true}).click();
    await page.getByRole('heading', {name:'Sign in',exact:true}).waitFor();
    assert.equal(await page.locator('.faculty-shell').count(),0);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed:true,checks:['ZIP-only controls','no legacy folder requests','search and empty states','file validation','upload pending lock','error retry','multipart package field','refresh after import','delete cancellation','two concurrent launch controls','test builder','student selection','four mobile layouts','signout'],screenshots:output}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
