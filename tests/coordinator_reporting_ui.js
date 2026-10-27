const assert = require('node:assert/strict');
const ui = require(process.argv[2]);
class Node {
  constructor(tag) { this.tag=tag; this.children=[]; this.value=''; }
  append(...nodes) { this.children.push(...nodes); if(this.tag==='select' && !this.value) this.value=nodes[0]?.value || ''; }
  prepend(...nodes) { this.children.unshift(...nodes); }
  setAttribute() {}
  addEventListener() {}
}
global.document = {createElement: tag => new Node(tag)};
const walk = node => [node, ...node.children.flatMap(walk)];
const render = (release, devices) => {
  const root = new Node('root');
  ui.renderClientUpdateRelease(root, release, devices);
  return walk(root);
};
const base = {release_id:'release', state:'pilot', client_version:'2.1.7', pilot_device_id:'pilot-id', devices:[]};
const devices = [{device_id:'other-id', label:'<LAB>', active:true}, {device_id:'pilot-id', label:'<LAB>', active:true}];
for (const state of ['pilot','published','withdrawn']) {
  const nodes = render({...base, state}, devices);
  const label = nodes.find(n => n.tag==='dt' && n.textContent==='Pilot computer');
  assert.ok(label, 'Selected pilot must be identified after selection');
  assert.ok(nodes.some(n => n.tag==='dd' && n.textContent==='<LAB> (pilot-id)'));
}
assert.ok(render({...base, devices:[{device_id:'pilot-id', label:'Release label'}]}, [])
  .some(n => n.tag==='dd' && n.textContent==='Release label (pilot-id)'));
assert.ok(render(base, []).some(n => n.tag==='dd' && n.textContent==='Unknown computer (pilot-id)'));
assert.ok(render({...base, devices:[{device_id:'pilot-id'}]}, devices)
  .some(n => n.tag==='dd' && n.textContent==='<LAB> (pilot-id)'));
const options = render({...base, state:'uploaded', pilot_device_id:null}, devices).filter(n => n.tag==='option');
assert.deepEqual(options.map(n => n.textContent), ['<LAB> (other-id)','<LAB> (pilot-id)']);
assert.equal(ui.clientUpdateStatusLabel({status:'Unknown'}), 'Unknown');
const unknown = render({...base, devices:[{device_id:'pilot-id', label:'LAB', status:'Unknown'}]}, devices);
assert.ok(unknown.some(n => n.tag==='td' && n.textContent==='Unknown'));
const guidance = unknown.filter(n => n.tag==='p').map(n => n.textContent).join(' ');
assert.match(guidance, /2\.1\.5/);
assert.match(guidance, /pending submissions.*one-time administrator installation/);
assert.match(guidance, /Unknown.*contact/);
assert.equal(unknown.find(n => n.tag==='button' && n.textContent==='Publish to all clients').disabled, true);
assert.equal(ui.canPublishClientUpdate({...base, devices:[{device_id:'pilot-id',stage:'healthy',installed_version:'2.1.5'}]}), false);
assert.equal(ui.canPublishClientUpdate({...base, devices:[{device_id:'pilot-id',stage:'healthy',installed_version:'2.1.7'}]}), true);
