/**
 * Unit tests for agentAPI.js
 *
 * Loading strategy: agentAPI.js uses a `new function()` constructor expression
 * assigned to Zotero.AgentAPI. We load it via vm.runInThisContext() after
 * pre-stubbing all globals. Each beforeEach re-runs the script to get a fresh
 * closure (resets _pollCount, _busy, _pendingSaves, etc.).
 *
 * Timer strategy: sinon fake timers control setTimeout/clearTimeout.
 * After tickAsync(), flush microtasks with await Promise.resolve() chains so
 * async _handleSave steps (tabs.create, _waitForReady, etc.) complete.
 * getTabInfo returns translators immediately so the event-driven translator
 * readiness check resolves as soon as the stability window completes.
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import vm from 'vm';
import sinon from 'sinon';
import { assert } from 'chai';

const __dirname = dirname(fileURLToPath(import.meta.url));
const AGENT_API_PATH = join(__dirname, '../../src/browserExt/agentAPI.js');
const agentAPICode = readFileSync(AGENT_API_PATH, 'utf8');

// ─── Helpers ────────────────────────────────────────────────────────────────

/** Flush microtask queue (Promise resolutions) */
async function flush(n = 30) {
	for (let i = 0; i < n; i++) await Promise.resolve();
}

function makeZoteroStub() {
	return {
		initDeferred: { promise: Promise.resolve() },
		Messaging: {
			sendMessage: sinon.stub(),
			receiveMessage: sinon.stub(),
		},
		Connector_Browser: {
			onZoteroButtonElementClick: sinon.stub(),
			onTranslators: sinon.stub(),
			setKeepServiceWorkerAlive: sinon.stub(),
			// Return translators immediately → _pollForTranslators resolves on first check
			// (no additional tickAsync needed for the translator poll loop)
			getTabInfo: sinon.stub().returns({ translators: [{ label: 'DOI' }] }),
		},
		debug: sinon.stub(),
		logError: sinon.stub(),
	};
}

function makeBrowserStub() {
	return {
		tabs: {
			create: sinon.stub().resolves({ id: 42, status: 'complete', title: 'Test Page' }),
			get: sinon.stub().resolves({ id: 42, status: 'complete', title: 'Test Page' }),
			remove: sinon.stub().resolves(),
			onUpdated: {
				addListener: sinon.stub(),
				removeListener: sinon.stub(),
			},
		},
		runtime: {
			getManifest: sinon.stub().returns({ version: '0.0.2' }),
		},
	};
}

function makeFetchStub(resultCb) {
	return sinon.stub().callsFake((url, opts) => {
		if (url.includes('/pending')) {
			return Promise.resolve({
				status: 200,
				ok: true,
				json: () => Promise.resolve({
					action: 'save',
					url: 'https://example.com/paper',
					request_id: 'req-test',
				}),
			});
		}
		if (url.includes('/result')) {
			if (resultCb) resultCb(JSON.parse(opts.body));
			return Promise.resolve({ status: 200, ok: true });
		}
		// /heartbeat, /pending 204, Zotero ping
		return Promise.resolve({ status: 204, ok: true });
	});
}

function makeRecentItem(key, title) {
	return {
		key,
		data: {
			key,
			title,
		},
	};
}

function loadAgentAPI() {
	vm.runInThisContext(agentAPICode);
}

/**
 * Run a full save cycle and return the result posted to /result.
 * Uses fake timers. getTabInfo returns translators immediately.
 * Completion is triggered by calling completionFn(patchedSend, patchedReceive).
 */
async function runSaveCycle({ command, completionFn, fetchOverride } = {}) {
	const clock = sinon.useFakeTimers({ shouldClearNativeTimers: true });
	let resultPosted = null;

	globalThis.Zotero = makeZoteroStub();
	globalThis.browser = makeBrowserStub();

	const cmd = command || {
		action: 'save',
		url: 'https://example.com/paper',
		request_id: 'req-test',
	};

	globalThis.fetch = fetchOverride || sinon.stub().callsFake((url, opts) => {
		if (url.includes('/pending')) {
			return Promise.resolve({
				status: 200, ok: true,
				json: () => Promise.resolve(cmd),
			});
		}
		if (url.includes('/result')) {
			resultPosted = JSON.parse(opts.body);
			return Promise.resolve({ status: 200, ok: true });
		}
		return Promise.resolve({ status: 204, ok: true });
	});

	loadAgentAPI();
	await Zotero.AgentAPI.init();
	await flush();

	// Tick past poll interval → _poll fires → _handleSave starts
	await clock.tickAsync(2001);
	await flush();
	// _waitForReady() uses a 4000ms stability window before translator readiness check
	await clock.tickAsync(4000);
	// Flush: tabs.create → _waitForReady → local API snapshot → tabs.get → onReady → _pendingSaves.set
	await flush();

	const patchedSend = Zotero.Messaging.sendMessage;
	const patchedReceive = Zotero.Messaging.receiveMessage;

	if (completionFn) {
		await completionFn(patchedSend, patchedReceive);
	} else {
		// Default: include an item key so second-handshake does not block the test
		patchedSend('progressWindow.itemProgress',
			{ title: 'Test Page', key: 'TESTKEY42' }, { id: 42 }, 0);
		patchedSend('progressWindow.done', [true], { id: 42 }, 0);
	}

	await flush();
	await clock.tickAsync(100);
	await flush();

	Zotero.AgentAPI.destroy();
	clock.restore();

	return { resultPosted, patchedSend, patchedReceive };
}

// ─── Test Suite ──────────────────────────────────────────────────────────────

describe('AgentAPI', function() {
	this.timeout(10000);

	let consoleStub;

	beforeEach(function() {
		// Suppress load-time console.log from agentAPI.js line 27
		consoleStub = sinon.stub(console, 'log');

		// Set up base globals (tests that use runSaveCycle override these)
		globalThis.Zotero = makeZoteroStub();
		globalThis.browser = makeBrowserStub();
		globalThis.fetch = sinon.stub().resolves({ status: 204, ok: true });

		// Fresh closure for each test
		loadAgentAPI();
	});

	afterEach(function() {
		if (globalThis.Zotero && globalThis.Zotero.AgentAPI) {
			globalThis.Zotero.AgentAPI.destroy();
		}
		consoleStub.restore();
		sinon.restore();
	});

	// ── init ──────────────────────────────────────────────────────────────────

	describe('init()', function() {
		it('installs sendMessage monkey-patch', async function() {
			const originalSend = Zotero.Messaging.sendMessage;
			await Zotero.AgentAPI.init();
			assert.notStrictEqual(Zotero.Messaging.sendMessage, originalSend,
				'sendMessage should be replaced by monkey-patch');
		});

		it('installs receiveMessage monkey-patch', async function() {
			const originalReceive = Zotero.Messaging.receiveMessage;
			await Zotero.AgentAPI.init();
			assert.notStrictEqual(Zotero.Messaging.receiveMessage, originalReceive,
				'receiveMessage should be replaced by monkey-patch');
		});
	});

	// ── receiveMessage patch ──────────────────────────────────────────────────

	describe('receiveMessage patch', function() {
		it('unwraps Messaging.sendMessage wrapper and detects progressWindow.done (AC-1)', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend, patchedReceive) => {
					patchedSend('progressWindow.itemProgress',
						{ title: 'My Paper', key: 'ABCD1234' }, { id: 42 }, 0);
					// Fire receiveMessage with the wrapped inject-side format
					await patchedReceive(
						'Messaging.sendMessage',
						['progressWindow.done', [true]],
						{ id: 42 },
						0
					);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should have been posted to /result');
			assert.strictEqual(resultPosted._detected_via, 'receiveMessage',
				'completion should be detected via receiveMessage patch');
			assert.strictEqual(resultPosted.success, true);
		});

		it('forwards original arguments to _originalReceive unchanged (AC-2)', async function() {
			const originalReceive = sinon.stub().resolves();
			Zotero.Messaging.receiveMessage = originalReceive;

			await Zotero.AgentAPI.init();
			const patched = Zotero.Messaging.receiveMessage;

			const wrappedArgs = ['someOtherMessage', [1, 2, 3]];
			const tab = { id: 99 };
			await patched('Messaging.sendMessage', wrappedArgs, tab, 0);

			assert.isTrue(originalReceive.calledOnce, '_originalReceive should be called once');
			// Must pass ORIGINAL messageName, not unwrapped
			assert.strictEqual(originalReceive.firstCall.args[0], 'Messaging.sendMessage',
				'original messageName must be forwarded unchanged');
			// Must pass ORIGINAL args, not unwrapped
			assert.strictEqual(originalReceive.firstCall.args[1], wrappedArgs,
				'original args must be forwarded unchanged');
		});

		it('ignores non-progressWindow.done wrapped messages without error', async function() {
			await Zotero.AgentAPI.init();
			const patched = Zotero.Messaging.receiveMessage;

			// Should not throw; tab 42 has no pending save anyway
			await patched(
				'Messaging.sendMessage',
				['progressWindow.itemProgress', [{ title: 'test' }]],
				{ id: 42 },
				0
			);
			// No assertion needed — verifying no throw
		});

		it('does not fire on direct (non-wrapped) messages that are not progressWindow.done', async function() {
			await Zotero.AgentAPI.init();
			const patched = Zotero.Messaging.receiveMessage;

			// Non-wrapped, non-done message — should pass through silently
			await patched('someRandomMessage', ['data'], { id: 42 }, 0);
		});
	});

	// ── sendMessage patch ─────────────────────────────────────────────────────

	describe('sendMessage patch', function() {
		it('resolves pending save on progressWindow.done (AC-3)', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress',
						{ title: 'My Paper', key: 'ABCD1234' }, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.strictEqual(resultPosted._detected_via, 'sendMessage');
			assert.strictEqual(resultPosted.success, true);
		});

		it('captures title and key from progressWindow.itemProgress (AC-4)', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend) => {
					// itemProgress before done
					patchedSend('progressWindow.itemProgress',
						{ title: 'My Paper', key: 'ABCD1234' }, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.strictEqual(resultPosted.title, 'My Paper',
				'title should be captured from itemProgress');
			assert.strictEqual(resultPosted.item_key, 'ABCD1234',
				'item_key should be captured from itemProgress');
		});

		it('does not confirm PDF from attachment icon alone', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						key: 'ABCD1234',
						iconSrc: 'attachment-pdf.png',
					}, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.isFalse(resultPosted.pdf_connector_confirmed,
				'attachment-pdf without completion should not confirm PDF');
			assert.isFalse(resultPosted.pdf_failed,
				'attachment-pdf without failure should not set pdf_failed');
		});

		it('confirms PDF only when the PDF row reaches progress 100', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						key: 'ABCD1234',
						iconSrc: 'attachment-pdf.png',
					}, { id: 42 }, 0);
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						key: 'ABCD1234',
						progress: 100,
					}, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.isTrue(resultPosted.pdf_connector_confirmed,
				'PDF row should be confirmed only after completion');
			assert.isFalse(resultPosted.pdf_failed);
		});

		it('ignores generic cross.png without prior PDF row context', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'Snapshot',
						key: 'ABCD1234',
						iconSrc: 'cross.png',
					}, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.isFalse(resultPosted.pdf_failed,
				'generic cross.png should not be treated as a PDF failure');
			assert.isFalse(resultPosted.pdf_connector_confirmed);
		});

		it('serializes pdf_failed from entry state when a PDF row later fails', async function() {
			const { resultPosted } = await runSaveCycle({
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						key: 'ABCD1234',
						iconSrc: 'attachment-pdf.png',
					}, { id: 42 }, 0);
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						key: 'ABCD1234',
						iconSrc: 'cross.png',
					}, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.isTrue(resultPosted.pdf_failed,
				'pdf_failed should be posted from entry state even when done resolved the save');
			assert.isFalse(resultPosted.pdf_connector_confirmed);
		});

		it('early-resolves only for a PDF row failure', async function() {
			let recentItemsResponses = [
				[],
				[makeRecentItem('NEWKEY123', 'My Paper')],
			];
			let resultPosted = null;
			const fetchOverride = sinon.stub().callsFake((url, opts) => {
				if (url.includes('/pending')) {
					return Promise.resolve({
						status: 200,
						ok: true,
						json: () => Promise.resolve({
							action: 'save',
							url: 'https://example.com/paper',
							request_id: 'req-pdf-cross',
						}),
					});
				}
				if (url.includes('/api/users/0/items/top')) {
					let payload = recentItemsResponses.shift() || [];
					return Promise.resolve({
						status: 200,
						ok: true,
						json: () => Promise.resolve(payload),
					});
				}
				if (url.includes('/result')) {
					resultPosted = JSON.parse(opts.body);
					return Promise.resolve({ status: 200, ok: true });
				}
				return Promise.resolve({ status: 204, ok: true });
			});

			await runSaveCycle({
				fetchOverride,
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						iconSrc: 'attachment-pdf.png',
					}, { id: 42 }, 0);
					patchedSend('progressWindow.itemProgress', {
						id: 1,
						title: 'My Paper',
						iconSrc: 'cross.png',
					}, { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.strictEqual(resultPosted._detected_via, 'itemProgress_cross');
			assert.isTrue(resultPosted.pdf_failed);
			assert.strictEqual(resultPosted.item_key, 'NEWKEY123');
		});

		it('backfills item_key from Zotero local API after translator save', async function() {
			let recentItemsResponses = [
				[],
				[makeRecentItem('NEWKEY123', 'My Paper')],
			];
			let resultPosted = null;
			const fetchOverride = sinon.stub().callsFake((url, opts) => {
				if (url.includes('/pending')) {
					return Promise.resolve({
						status: 200,
						ok: true,
						json: () => Promise.resolve({
							action: 'save',
							url: 'https://example.com/paper',
							request_id: 'req-local-api',
						}),
					});
				}
				if (url.includes('/api/users/0/items/top')) {
					let payload = recentItemsResponses.shift() || [];
					return Promise.resolve({
						status: 200,
						ok: true,
						json: () => Promise.resolve(payload),
					});
				}
				if (url.includes('/result')) {
					resultPosted = JSON.parse(opts.body);
					return Promise.resolve({ status: 200, ok: true });
				}
				return Promise.resolve({ status: 204, ok: true });
			});

			await runSaveCycle({
				fetchOverride,
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress',
						{ title: 'My Paper' }, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.strictEqual(resultPosted.item_key, 'NEWKEY123',
				'item_key should be discovered from the Zotero local API');
		});

		it('falls back to the first new item when local API discovery is ambiguous', async function() {
			let recentItemsResponses = [
				[makeRecentItem('OLDKEY001', 'Old Paper')],
				[
					makeRecentItem('NEWKEY111', 'First Different Title'),
					makeRecentItem('NEWKEY222', 'Second Different Title'),
					makeRecentItem('OLDKEY001', 'Old Paper'),
				],
			];
			let resultPosted = null;
			const fetchOverride = sinon.stub().callsFake((url, opts) => {
				if (url.includes('/pending')) {
					return Promise.resolve({
						status: 200,
						ok: true,
						json: () => Promise.resolve({
							action: 'save',
							url: 'https://example.com/paper',
							request_id: 'req-ambiguous',
						}),
					});
				}
				if (url.includes('/api/users/0/items/top')) {
					let payload = recentItemsResponses.shift() || [];
					return Promise.resolve({
						status: 200,
						ok: true,
						json: () => Promise.resolve(payload),
					});
				}
				if (url.includes('/result')) {
					resultPosted = JSON.parse(opts.body);
					return Promise.resolve({ status: 200, ok: true });
				}
				return Promise.resolve({ status: 204, ok: true });
			});

			await runSaveCycle({
				fetchOverride,
				completionFn: async (patchedSend) => {
					patchedSend('progressWindow.itemProgress',
						{ title: 'Saved Paper' }, { id: 42 }, 0);
					patchedSend('progressWindow.done', [true], { id: 42 }, 0);
					await flush();
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.strictEqual(resultPosted.item_key, 'NEWKEY111',
				'item_key should fall back to the first new candidate when ambiguous');
		});

		it('forwards all messages to the original sendMessage handler', async function() {
			const originalSend = sinon.stub();
			Zotero.Messaging.sendMessage = originalSend;

			await Zotero.AgentAPI.init();
			const patched = Zotero.Messaging.sendMessage;

			patched('someMessage', ['arg1'], { id: 99 }, 0);

			assert.isTrue(originalSend.calledOnce, 'original sendMessage should be called');
			assert.strictEqual(originalSend.firstCall.args[0], 'someMessage');
		});
	});

	// ── _handleSave collection_key/tags ───────────────────────────────────────

	describe('_handleSave collection_key and tags routing', function() {
		it('posts collection_key and tags from command to /result (AC-5)', async function() {
			const { resultPosted } = await runSaveCycle({
				command: {
					action: 'save',
					url: 'https://example.com/paper',
					request_id: 'req-ac5',
					collection_key: 'XYZ789',
					tags: ['machine-learning', 'NLP'],
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.strictEqual(resultPosted.collection_key, 'XYZ789',
				'collection_key should be echoed from command');
			assert.deepEqual(resultPosted.tags, ['machine-learning', 'NLP'],
				'tags should be echoed from command');
		});

		it('defaults collection_key to null and tags to [] when absent (AC-6)', async function() {
			const { resultPosted } = await runSaveCycle({
				command: {
					action: 'save',
					url: 'https://example.com/paper',
					request_id: 'req-ac6',
					// collection_key and tags intentionally absent
				},
			});

			assert.isNotNull(resultPosted, 'result should be posted');
			assert.isNull(resultPosted.collection_key,
				'collection_key should default to null');
			assert.deepEqual(resultPosted.tags, [],
				'tags should default to []');
		});
	});

	// ── _poll heartbeat ───────────────────────────────────────────────────────

	describe('_poll heartbeat', function() {
		it('sends heartbeat exactly once after 5 poll cycles (AC-7)', async function() {
			const clock = sinon.useFakeTimers({ shouldClearNativeTimers: true });

			globalThis.Zotero = makeZoteroStub();
			globalThis.browser = makeBrowserStub();

			const heartbeatUrls = [];
			globalThis.fetch = sinon.stub().callsFake((url) => {
				if (url.includes('/heartbeat')) heartbeatUrls.push(url);
				return Promise.resolve({ status: 204, ok: true });
			});

			loadAgentAPI();
			await Zotero.AgentAPI.init();
			await flush();

			// 5 poll cycles: _pollCount increments after each /pending fetch
			// heartbeat fires when _pollCount % 5 === 0, i.e. after the 5th poll
			for (let i = 0; i < 5; i++) {
				await clock.tickAsync(2001);
				await flush();
			}

			Zotero.AgentAPI.destroy();
			clock.restore();

			assert.strictEqual(heartbeatUrls.length, 1,
				'heartbeat should fire exactly once after 5 polls');
		});
	});

	// ── translator readiness ──────────────────────────────────────────────────

	describe('translator readiness', function() {
		it('proceeds once translators are already available after stability', async function() {
			const clock = sinon.useFakeTimers({ shouldClearNativeTimers: true });

			globalThis.Zotero = makeZoteroStub();
			globalThis.browser = makeBrowserStub();

			let resultPosted = null;
			globalThis.fetch = sinon.stub().callsFake((url, opts) => {
				if (url.includes('/pending')) return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ action: 'save', url: 'https://example.com', request_id: 'rpt' }) });
				if (url.includes('/result')) { resultPosted = JSON.parse(opts.body); return Promise.resolve({ status: 200, ok: true }); }
				return Promise.resolve({ status: 204, ok: true });
			});

			loadAgentAPI();
			await Zotero.AgentAPI.init();
			await flush();

			// Trigger poll
			await clock.tickAsync(2001);
			await flush();
			// Wait through the stability window before the translator readiness check
			await clock.tickAsync(4000);
			await flush();

			// Trigger completion
			Zotero.Messaging.sendMessage('progressWindow.itemProgress',
				{ title: 'My Paper', key: 'ABCD1234' }, { id: 42 }, 0);
			Zotero.Messaging.sendMessage('progressWindow.done', [true], { id: 42 }, 0);
			await flush();
			await clock.tickAsync(100);
			await flush();

			Zotero.AgentAPI.destroy();
			clock.restore();

			assert.isAtLeast(Zotero.Connector_Browser.getTabInfo.callCount, 1,
				'getTabInfo should be checked once after stability');
			assert.isNotNull(resultPosted, 'save should complete after translators detected');
		});

		it('waits for the onTranslators hook when translators are not ready yet', async function() {
			const clock = sinon.useFakeTimers({ shouldClearNativeTimers: true });

			globalThis.Zotero = makeZoteroStub();
			globalThis.browser = makeBrowserStub();

			Zotero.Connector_Browser.getTabInfo = sinon.stub().returns(null);

			let resultPosted = null;
			globalThis.fetch = sinon.stub().callsFake((url, opts) => {
				if (url.includes('/pending')) return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ action: 'save', url: 'https://example.com', request_id: 'rpt2' }) });
				if (url.includes('/result')) { resultPosted = JSON.parse(opts.body); return Promise.resolve({ status: 200, ok: true }); }
				return Promise.resolve({ status: 204, ok: true });
			});

			loadAgentAPI();
			await Zotero.AgentAPI.init();
			await flush();

			await clock.tickAsync(2001);
			await flush();
			await clock.tickAsync(4000);
			await flush();

			assert.isNull(resultPosted, 'save should still be waiting for translator event');
			Zotero.Connector_Browser.onTranslators([{ label: 'DOI' }], 1, null, { id: 42 }, 0);
			await flush();

			// Trigger completion
			Zotero.Messaging.sendMessage('progressWindow.itemProgress',
				{ title: 'My Paper', key: 'ABCD1234' }, { id: 42 }, 0);
			Zotero.Messaging.sendMessage('progressWindow.done', [true], { id: 42 }, 0);
			await flush();
			await clock.tickAsync(100);
			await flush();

			Zotero.AgentAPI.destroy();
			clock.restore();

			assert.strictEqual(Zotero.Connector_Browser.getTabInfo.callCount, 1,
				'event-driven readiness should not poll repeatedly');
			assert.isNotNull(resultPosted, 'save should complete after onTranslators fires');
		});

		it('fails fast with no_translator after the translator wait timeout', async function() {
			const clock = sinon.useFakeTimers({ shouldClearNativeTimers: true });

			globalThis.Zotero = makeZoteroStub();
			globalThis.browser = makeBrowserStub();
			Zotero.Connector_Browser.getTabInfo = sinon.stub().returns(null);

			let resultPosted = null;
			globalThis.fetch = sinon.stub().callsFake((url, opts) => {
				if (url.includes('/pending')) return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ action: 'save', url: 'https://example.com', request_id: 'rpt3' }) });
				if (url.includes('/result')) { resultPosted = JSON.parse(opts.body); return Promise.resolve({ status: 200, ok: true }); }
				return Promise.resolve({ status: 204, ok: true });
			});

			loadAgentAPI();
			await Zotero.AgentAPI.init();
			await flush();

			await clock.tickAsync(2001);
			await flush();
			await clock.tickAsync(4000);
			await flush();
			await clock.tickAsync(20000);
			await flush();

			Zotero.AgentAPI.destroy();
			clock.restore();

			assert.strictEqual(Zotero.Connector_Browser.getTabInfo.callCount, 1);
			assert.isNotNull(resultPosted, 'bridge should receive a no_translator result');
			assert.strictEqual(resultPosted.success, false);
			assert.strictEqual(resultPosted.error_code, 'no_translator');
		});
	});

	// ── destroy ───────────────────────────────────────────────────────────────

	describe('destroy()', function() {
		it('stops polling after destroy() is called', async function() {
			const clock = sinon.useFakeTimers({ shouldClearNativeTimers: true });

			globalThis.Zotero = makeZoteroStub();
			globalThis.browser = makeBrowserStub();
			globalThis.fetch = sinon.stub().resolves({ status: 204, ok: true });

			loadAgentAPI();
			await Zotero.AgentAPI.init();
			await flush();

			// Let one poll fire
			await clock.tickAsync(2001);
			await flush();
			const callsBefore = globalThis.fetch.callCount;

			// Destroy
			Zotero.AgentAPI.destroy();

			// Tick past several more intervals — no new polls should fire
			await clock.tickAsync(8000);
			await flush();
			const callsAfter = globalThis.fetch.callCount;

			clock.restore();

			assert.strictEqual(callsBefore, callsAfter,
				'no new fetch calls should happen after destroy()');
		});
	});
});
