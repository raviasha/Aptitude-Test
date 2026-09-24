const assert = require('node:assert/strict');
const { mathText, mathEsc } = require('../static/app.js');

assert.equal(mathText('2.6\u03054\u0305'), '2.(64)');
assert.equal(mathText('2.64'), '2.64');
assert.notEqual(mathEsc('2.64'), mathEsc('2.6\u03054\u0305'));
assert.equal(mathText('112 × 5⁴'), '112 × 5⁴');
assert.equal(mathEsc('<b>2.6\u03054\u0305</b>'), '&lt;b&gt;2.(64)&lt;/b&gt;');
assert.equal(mathText('0.16\u0305'), '0.1(6)');
assert.equal(mathText('0.12\u03053'), '0.1(2)3');
assert.equal(mathText('2.6\u03054\u0305 + 1.3\u0305'), '2.(64) + 1.(3)');
assert.equal(mathEsc('2.6\u03054\u0305 < 3'), '2.(64) &lt; 3');
