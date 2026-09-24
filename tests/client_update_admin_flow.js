const ui = require(process.argv[2]);

const devices = [
  {device_id: 'active', label: '<Lab A>', active: true},
  {device_id: 'revoked', label: 'Lab B', active: false},
];
const release = {
  client_version: '2.1.0', state: 'pilot', pilot_device_id: 'active',
  devices: [{device_id: 'active', stage: 'healthy', installed_version: '2.1.0', status: 'Updated'}],
};
process.stdout.write(JSON.stringify({
  pilots: ui.eligiblePilotDevices(devices),
  publish: ui.canPublishClientUpdate(release),
  wrongVersion: ui.canPublishClientUpdate({...release, devices:[{device_id:'active', stage:'healthy', installed_version:'2.0.0'}]}),
  labels: ['Updated','Downloading','Waiting','Offline','Failed'].map(status => ui.clientUpdateStatusLabel({status})),
}));
