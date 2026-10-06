'use strict';
const assert = require('node:assert/strict');
const { createBrowserPresence } = require(process.argv[2]);
const sockets = [], timers = [];
let ready = 0, unavailable = 0;
const presence = createBrowserPresence({
  openSocket() { const socket = { close() { this.onclose?.({code: 1000}); } }; sockets.push(socket); return socket; },
  schedule(fn) { timers.push(fn); return fn; }, cancel(fn) { const i = timers.indexOf(fn); if (i >= 0) timers.splice(i, 1); },
  onReady() { ready++; }, onUnavailable() { unavailable++; },
});
presence.start();
assert.equal(sockets.length, 1);
assert.equal(ready, 0, 'no state/login before presence acknowledged');
sockets[0].onmessage({data: '{"state":"connected"}'});
assert.equal(ready, 1);
presence.start(); // hidden/online/pageshow events must not create duplicate sockets
assert.equal(sockets.length, 1);
sockets[0].onclose({code: 1006});
assert.equal(timers.length, 1);
timers.shift()();
assert.equal(sockets.length, 2);
sockets[1].onmessage({data: '{"state":"connected"}'});
assert.equal(ready, 2);
presence.suspend();
assert.equal(timers.length, 0, 'pagehide never reconnects a closed document');
presence.start();
assert.equal(sockets.length, 3, 'pageshow resumes presence');
sockets[2].onclose({code: 1013});
assert.equal(unavailable, 1);
assert.equal(timers.length, 0, 'committed close tells user to relaunch');
console.log('Browser presence behavior passed');
