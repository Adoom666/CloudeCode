// Node test for macOS/network-interfaces.js - which addresses the Bind IP
// menu offers.
//
// WHY THIS FILE EXISTS
//
// The bind menu filtered interfaces by NAME, with /^utun/ on the blocklist
// and a comment claiming those interfaces "never carry routable IPv4 even if
// one shows up". The claim was false. utun4 on the user's machine carries
// 100.113.217.77, and the menu silently refused to offer it, so he could not
// bind the server to his Tailscale address.
//
// Note the SHAPE of that failure, because it is why no test caught it: there
// was no error, no exception and no empty state. The address was simply
// absent from a list, which looks exactly like not having one. The only way
// to catch it is to assert that a specific address IS present given a
// specific machine description, which is what the first test below does.
//
// The interface table is injected rather than read from the host, so these
// assertions describe machines this test is not running on: a Mac with
// AirDrop up, one with a VPN, one with a link-local address and nothing else.
//
// Run with: node tests/test_network_interfaces.node.mjs

import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const net = require(path.join(repoRoot, 'macOS', 'network-interfaces.js'));

let passed = 0;
const failures = [];

/**
 * Run one named assertion block.
 *
 * @param {string} name - Description of the behaviour.
 * @param {() => void} fn - Assertions.
 * @returns {void}
 */
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    failures.push({ name, error });
    console.log('  FAIL ' + name + ': ' + error.message);
  }
}

/** A realistic macOS interface table, matching the user's actual machine. */
const REAL_MAC = {
  lo0: [
    { address: '127.0.0.1', family: 'IPv4', internal: true },
    { address: '::1', family: 'IPv6', internal: true },
  ],
  en0: [{ address: '10.0.1.86', family: 'IPv4', internal: false }],
  // AirDrop: link-local IPv6 only, no IPv4 at all.
  awdl0: [{ address: 'fe80::1c churn', family: 'IPv6', internal: false }],
  llw0: [{ address: 'fe80::2', family: 'IPv6', internal: false }],
  // Apple Silicon internal radios.
  anpi0: [{ address: 'fe80::3', family: 'IPv6', internal: false }],
  ap1: [{ address: 'fe80::4', family: 'IPv6', internal: false }],
  // Tailscale.
  utun4: [{ address: '100.113.217.77', family: 'IPv4', internal: false }],
};

console.log('network-interfaces: the regression');

test('the Tailscale address on utun4 IS offered', () => {
  const ips = net.listBindableIps(REAL_MAC).map((r) => r.ip);
  assert.ok(
    ips.includes('100.113.217.77'),
    'utun4 carries a routable IPv4 and must be bindable; a name blocklist ' +
      'used to drop it silently. Got: ' + JSON.stringify(ips)
  );
});

test('the utun interface is reported under its real name', () => {
  const row = net.listBindableIps(REAL_MAC).find((r) => r.ip === '100.113.217.77');
  assert.equal(row.iface, 'utun4');
});

test('the ordinary LAN address is still offered', () => {
  const ips = net.listBindableIps(REAL_MAC).map((r) => r.ip);
  assert.ok(ips.includes('10.0.1.86'));
});

console.log('network-interfaces: what stays excluded, and WHY');

test('AirDrop and the internal radios are excluded by carrying no IPv4', () => {
  const names = net.listBindableIps(REAL_MAC).map((r) => r.iface);
  for (const excluded of ['awdl0', 'llw0', 'anpi0', 'ap1']) {
    assert.ok(
      !names.includes(excluded),
      excluded + ' must not be offered'
    );
  }
});

test('a pseudo-interface that DID carry a routable IPv4 would be offered', () => {
  // The point of the change: the rule is about the address, not the name.
  // If AirDrop ever carried a real address, refusing it by name would be the
  // same bug again.
  const ips = net
    .listBindableIps({ awdl0: [{ address: '10.9.9.9', family: 'IPv4', internal: false }] })
    .map((r) => r.ip);
  assert.deepEqual(ips, ['10.9.9.9']);
});

test('loopback is excluded because it is internal', () => {
  const ips = net.listBindableIps(REAL_MAC).map((r) => r.ip);
  assert.ok(!ips.includes('127.0.0.1'));
});

test('link-local 169.254 addresses are excluded', () => {
  const ips = net
    .listBindableIps({ en5: [{ address: '169.254.10.20', family: 'IPv4', internal: false }] })
    .map((r) => r.ip);
  assert.deepEqual(ips, [], 'a self-assigned address is not reachable');
});

test('IPv6 addresses are never offered as bind targets', () => {
  const ips = net
    .listBindableIps({ en0: [{ address: 'fd7a:115c:a1e0::1', family: 'IPv6', internal: false }] })
    .map((r) => r.ip);
  assert.deepEqual(ips, []);
});

test('a numeric family of 4 is accepted, as newer Node reports it', () => {
  const ips = net
    .listBindableIps({ en0: [{ address: '10.1.2.3', family: 4, internal: false }] })
    .map((r) => r.ip);
  assert.deepEqual(ips, ['10.1.2.3'], 'Node 18+ reports family as a number');
});

test('a malformed or empty table yields an empty list, not a throw', () => {
  assert.deepEqual(net.listBindableIps({}), []);
  assert.deepEqual(net.listBindableIps({ en0: null }), []);
  assert.deepEqual(net.listBindableIps({ en0: [null, undefined] }), []);
});

test('results are sorted by interface name for a stable menu', () => {
  const names = net
    .listBindableIps({
      utun4: [{ address: '100.113.217.77', family: 'IPv4', internal: false }],
      en0: [{ address: '10.0.1.86', family: 'IPv4', internal: false }],
    })
    .map((r) => r.iface);
  assert.deepEqual(names, ['en0', 'utun4']);
});

console.log('network-interfaces: Tailscale labelling');

test('the CGNAT range 100.64.0.0/10 is recognised', () => {
  for (const ip of ['100.64.0.0', '100.113.217.77', '100.127.255.255']) {
    assert.equal(net.isTailscaleIp(ip), true, ip + ' should be Tailscale');
  }
});

test('addresses just outside the CGNAT range are NOT labelled Tailscale', () => {
  for (const ip of ['100.63.255.255', '100.128.0.1', '10.0.1.86', '192.168.1.5']) {
    assert.equal(net.isTailscaleIp(ip), false, ip + ' should not be Tailscale');
  }
});

test('labelling never decides whether an address is offered', () => {
  // A Tailscale address and a LAN address are selected by the same rule.
  const ips = net.listBindableIps(REAL_MAC).map((r) => r.ip).sort();
  assert.deepEqual(ips, ['10.0.1.86', '100.113.217.77']);
});

console.log('');
console.log('network-interfaces: ' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length > 0 ? 1 : 0);
