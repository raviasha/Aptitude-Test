const assert = require('node:assert/strict');
const {importBankFiles, bankImportReport} = require('../static/app.js');

(async () => {
  const files = ['first.zip','broken.zip','last.zip'].map(name => new File(['zip fixture'],name));
  const calls = [], progress = [];
  let active = 0;
  const results = await importBankFiles(files, true, async (url, options) => {
    assert.equal(++active,1, 'imports must be sequential');
    assert.equal(url,'/api/admin/question-banks/import-package');
    assert.equal(options.body.get('replace_existing'),'true');
    const name = options.body.get('package_file').name;
    calls.push(name);
    await new Promise(resolve => setTimeout(resolve,5));
    active--;
    if (name === 'broken.zip') throw new Error('<invalid package>');
    return {bank_name:name,question_count:12};
  }, (index,total,name) => progress.push([index,total,name]));
  assert.deepEqual(calls, files.map(file => file.name));
  assert.deepEqual(results.map(result => result.ok),[true,false,true]);
  assert.deepEqual(progress.map(row => row[0]),[1,2,3]);
  assert.match(bankImportReport(results),/2 succeeded, 1 failed/);
  assert.match(bankImportReport(results),/&lt;invalid package&gt;/);
  await importBankFiles([files[0]],false,async (_, options) => {
    assert.equal(options.body.get('replace_existing'),'false');
    return {bank_name:'One',question_count:1};
  });
  console.log('Bulk import: sequential uploads, continuation after failure, progress, replacement flag and escaped report passed.');
})().catch(error => { console.error(error); process.exitCode=1; });
