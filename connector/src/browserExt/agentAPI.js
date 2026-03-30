/*
    ***** BEGIN LICENSE BLOCK *****

    Copyright © 2026 ZotPilot Contributors

    This file is part of ZotPilot Connector (a fork of Zotero Connector).

    ZotPilot Connector is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    ***** END LICENSE BLOCK *****
*/

/**
 * Agent API — enables AI agents to trigger Zotero saves via HTTP polling.
 *
 * Polls GET http://127.0.0.1:2619/pending for save commands from ZotPilot bridge.
 * On command: opens tab → waits for translator detection → triggers save via
 * onZoteroButtonElementClick → detects completion via sendMessage monkey-patch
 * on progressWindow.done → posts result back to bridge.
 *
 * The 2-second poll interval also serves as MV3 service worker keep-alive.
 */
// Task 1.1: timestamp at load time
console.log("[ZotPilot] agentAPI.js loaded at " + Date.now());

Zotero.AgentAPI = new function() {
	const BRIDGE_URL = "http://127.0.0.1:2619";
	const ZOTERO_LOCAL_API_URL = "http://127.0.0.1:23119/api/users/0/items/top";
	const POLL_INTERVAL = 2000;
	const SAVE_TIMEOUT_MS = 60000; // Task 1.2: 60s; reports "unconfirmed" not false success
	const HEARTBEAT_INTERVAL_MS = 10000; // Independent heartbeat every 10s — not tied to poll loop
	const RECENT_ITEMS_LIMIT = 10;
	const LOCAL_API_TIMEOUT_MS = 5000;
	const ANTI_BOT_PATTERNS = [
		"just a moment",
		"请稍候",
		"请稍等",
		"verify you are human",
		"access denied",
		"please verify",
		"robot check",
		"cloudflare",
		"security check",
		"captcha",
		"checking your browser",
		"one more step",
	];
	// Stability windows for JS-redirect detection in _waitForReady.
	// After status=complete, wait this long with no new events before polling translators.
	// Reliability-first values: better to be slow than to fire on the wrong page.
	const STABILITY_WINDOW_MS          = 2000;  // all pages: covers CF/JS challenges that reload same URL
	const STABILITY_WINDOW_REDIRECT_MS = 4000;  // JS-redirect pages: longer window for multi-hop (verify→article)

	let _polling = false;
	let _pollTimer = null;
	let _heartbeatTimer = null; // Independent heartbeat timer — not tied to poll loop
	let _busy = false;

	// Map<tabId, {resolve, item_key, item_title}> — pending save completions
	let _pendingSaves = new Map();

	/**
	 * Start polling. Called after Zotero.initDeferred resolves.
	 *
	 * Task 1.2: Installs DUAL monkey-patches on the messaging layer:
	 *
	 * 1. sendMessage patch (PRIMARY): ALL progressWindow.done signals flow through
	 *    Zotero.Messaging.sendMessage — both content-script-originated (relayed via
	 *    MESSAGES config dispatch) and background-originated (contentTypeHandler.js).
	 *    This is the patch that actually fires.
	 *
	 * 2. receiveMessage patch (DEFENSE-IN-DEPTH): currently non-functional for
	 *    progressWindow.done because the inject-side sendMessage stub wraps the call
	 *    as ["Messaging.sendMessage", [...]] so receiveMessage sees messageName =
	 *    "Messaging.sendMessage", not "progressWindow.done". Retained as a safety
	 *    net against future upstream changes that might send progressWindow.done
	 *    directly via browser.runtime.sendMessage.
	 *
	 * Both patches always forward to the original handler — they only observe.
	 */
	// Map<tabId, resolve> — pending translator-ready waiters (event-driven, replaces polling)
	let _translatorWaiters = new Map();

	this.init = async function() {
		// Task 1.1: milestone — init start
		console.log("[ZotPilot] AgentAPI.init() called at " + Date.now() + ", awaiting Zotero.initDeferred...");
		await Zotero.initDeferred.promise;
		// Task 1.1: milestone — initDeferred resolved
		console.log("[ZotPilot] Zotero.initDeferred resolved at " + Date.now());

		// Hook onTranslators to signal event-driven translator readiness.
		// When Zotero detects translators for a tab, notify any waiting _handleSave.
		const _originalOnTranslators = Zotero.Connector_Browser.onTranslators.bind(Zotero.Connector_Browser);
		Zotero.Connector_Browser.onTranslators = function(translators, instanceID, contentType, tab, frameId) {
			const result = _originalOnTranslators(translators, instanceID, contentType, tab, frameId);
			if (translators && translators.length > 0 && _translatorWaiters.has(tab.id)) {
				Zotero.debug("[ZotPilot] onTranslators hook: translator ready for tab " + tab.id + " (" + translators[0].label + ")");
				const resolve = _translatorWaiters.get(tab.id);
				_translatorWaiters.delete(tab.id);
				resolve();
			}
			return result;
		};

		// --- receiveMessage patch (DEFENSE-IN-DEPTH, currently non-functional for
		//     progressWindow.done — see init() JSDoc above) ---
		const _originalReceive = Zotero.Messaging.receiveMessage.bind(Zotero.Messaging);
		Zotero.Messaging.receiveMessage = async function(messageName, args, tab, frameId) {
			// Unwrap inject-side messages: ["Messaging.sendMessage", [actualName, actualArgs]]
			let effectiveName = messageName;
			let effectiveArgs = args;
			if (messageName === "Messaging.sendMessage" && Array.isArray(args) && typeof args[0] === "string") {
				Zotero.debug("[ZotPilot] receiveMessage unwrapped: " + args[0]);
				effectiveName = args[0];
				effectiveArgs = args[1];
			}
			if (effectiveName === "progressWindow.done" && tab && _pendingSaves.has(tab.id)) {
				let entry = _pendingSaves.get(tab.id);
				_pendingSaves.delete(tab.id);
				let success = effectiveArgs && effectiveArgs[0];
				let error = effectiveArgs && effectiveArgs.length > 1 ? effectiveArgs[1] : null;
				Zotero.debug("[ZotPilot] completion via receiveMessage patch (defense-in-depth)");
				entry.resolve({ success: !!success, error, _via: "receiveMessage" });
			}
			// ALWAYS forward to original — never swallow messages
			return _originalReceive(messageName, args, tab, frameId);
		};

		// --- sendMessage patch (PRIMARY completion detector) ---
		const _originalSend = Zotero.Messaging.sendMessage.bind(Zotero.Messaging);
		Zotero.Messaging.sendMessage = function(messageName, args, tab, frameId) {
			// Task 1.2: catch progressWindow.done for save completion
			if (messageName === "progressWindow.done" && tab && _pendingSaves.has(tab.id)) {
				let entry = _pendingSaves.get(tab.id);
				_pendingSaves.delete(tab.id);
				let success = args && args[0];
				let error = args && args.length > 1 ? args[1] : null;
				Zotero.debug("[ZotPilot] completion via sendMessage patch (primary)");
				entry.resolve({ success: !!success, error, _via: "sendMessage" });
			}
			// Task 1.3: intercept itemProgress to capture title and item_key.
			// Args is an object: { sessionID, id, iconSrc, title, itemsLoaded, itemType }
			// For saveAsWebpage fallback/server-save paths, args.key can contain the real
			// Zotero item key. Standard translator saves usually emit itemProgress without
			// a key, so successful saves fall back to a post-save local API lookup.
			if (messageName === "progressWindow.itemProgress" && tab && _pendingSaves.has(tab.id)) {
				let entry = _pendingSaves.get(tab.id);
				let payload = args || {};
				if (payload.title && !entry.item_title) entry.item_title = payload.title;
				if (payload.key && !entry.item_key) entry.item_key = payload.key;
				// PDF/attachment download failed: Zotero sets iconSrc to cross.png.
				// Resolve immediately so the user can handle the verification rather
				// than waiting for the 60s timeout — item metadata was saved successfully.
				if (payload.iconSrc && payload.iconSrc.includes("cross.png") && !entry.item_key) {
					Zotero.debug("[ZotPilot] attachment failed (cross.png) for " + (payload.title || "unknown") + " — resolving with pdf_failed");
					_pendingSaves.delete(tab.id);
					entry.resolve({
						success: true,
						pdf_failed: true,
						error_code: "pdf_download_failed",
						error: "PDF download failed (anti-bot or access restriction). Item metadata was saved. Please download the PDF manually.",
						_via: "itemProgress_cross",
					});
				}
			}
			// ALWAYS forward — observe only, never modify args
			return _originalSend(messageName, args, tab, frameId);
		};

		_polling = true;
		_schedulePoll();
		// Independent heartbeat timer: runs every 10s regardless of _busy or _poll() blocking.
		// Fixes: _poll() suspends during _handleSave (up to 95s), which starved the heartbeat
		// and caused bridge to report extension_not_connected after any long save.
		_heartbeatTimer = setInterval(() => {
			_sendHeartbeat().catch(() => {});
		}, HEARTBEAT_INTERVAL_MS);
		// Task 1.1: milestone — polling started
		console.log("[ZotPilot] AgentAPI polling started at " + Date.now());
		Zotero.debug("AgentAPI: initialized, polling " + BRIDGE_URL);
	};

	this.destroy = function() {
		_polling = false;
		if (_pollTimer) {
			clearTimeout(_pollTimer);
			_pollTimer = null;
		}
		if (_heartbeatTimer) {
			clearInterval(_heartbeatTimer);
			_heartbeatTimer = null;
		}
	};

	function _schedulePoll() {
		if (!_polling) return;
		_pollTimer = setTimeout(_poll, POLL_INTERVAL);
	}

	async function _poll() {
		if (!_polling) return;
		if (_busy) {
			_schedulePoll();
			return;
		}
		try {
			let response = await fetch(BRIDGE_URL + "/pending", {
				method: "GET",
				headers: { "Accept": "application/json" },
			});
			console.log("[ZotPilot] poll /pending → " + response.status);
			if (response.status === 200) {
				let command = await response.json();
				console.log("[ZotPilot] received command:", JSON.stringify(command));
				if (command && command.url) {
					if (command.action === "preflight") {
						await _handlePreflight(command);
					}
					else if (command.action === "save") {
						await _handleSave(command);
					}
				}
			}
		} catch (e) {
			console.log("[ZotPilot] poll error: " + e.message);
		}
		_schedulePoll();
	}

	/**
	 * POST heartbeat to bridge. Called by independent setInterval (not poll loop).
	 * Checks Zotero connectivity by pinging localhost:23119/connector/ping.
	 * Fire-and-forget — failures are silently ignored.
	 */
	async function _sendHeartbeat() {
		let zoteroConnected = false;
		try {
			let resp = await fetch("http://127.0.0.1:23119/connector/ping", {
				method: "GET",
				signal: AbortSignal.timeout(2000),
			});
			zoteroConnected = resp.ok;
		} catch (e) {}

		await fetch(BRIDGE_URL + "/heartbeat", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				extension_version: browser.runtime.getManifest().version,
				zotero_connected: zoteroConnected,
			}),
		});
	}

	function _normalizeTitle(title) {
		return (title || "")
			.toLowerCase()
			.replace(/\s+/g, " ")
			.replace(/[^\p{L}\p{N}\s]/gu, "")
			.trim();
	}

	function _extractRecentItem(rawItem) {
		if (!rawItem || typeof rawItem !== "object") return null;
		let data = rawItem.data && typeof rawItem.data === "object" ? rawItem.data : rawItem;
		let key = rawItem.key || data.key || null;
		if (!key) return null;
		return {
			key,
			title: data.title || rawItem.title || "",
		};
	}

	function _pickBestMatchingItem(items, preferredTitles) {
		let normalizedPreferred = preferredTitles
			.map(_normalizeTitle)
			.filter(Boolean);
		if (!normalizedPreferred.length) return null;

		let exactMatches = items.filter((item) => {
			let normalizedTitle = _normalizeTitle(item.title);
			return normalizedTitle && normalizedPreferred.includes(normalizedTitle);
		});
		if (exactMatches.length === 1) {
			return exactMatches[0];
		}

		let fuzzyMatches = items.filter((item) => {
			let normalizedTitle = _normalizeTitle(item.title);
			return normalizedTitle && normalizedPreferred.some((preferred) => {
				return normalizedTitle.includes(preferred) || preferred.includes(normalizedTitle);
			});
		});
		if (fuzzyMatches.length === 1) {
			return fuzzyMatches[0];
		}

		return null;
	}

	async function _applyLocalRouting(itemKey, collectionKey, tags) {
		const base = "http://127.0.0.1:23119/api/users/0/items/" + itemKey;
		try {
			let resp = await fetch(base, {
				method: "GET",
				headers: { "Accept": "application/json", "Zotero-Allowed-Request": "1" },
				signal: AbortSignal.timeout(5000),
			});
			if (!resp.ok) {
				Zotero.debug("[ZotPilot] local routing GET failed: " + resp.status);
				return;
			}
			let item = await resp.json();
			let data = item.data || item;
			if (collectionKey) {
				let existing = new Set(data.collections || []);
				existing.add(collectionKey);
				data.collections = Array.from(existing);
			}
			if (tags && tags.length) {
				let existing = new Set((data.tags || []).map(t => t.tag));
				for (let t of tags) existing.add(t);
				data.tags = Array.from(existing).map(t => ({ tag: t }));
			}
			let patch = await fetch(base, {
				method: "PATCH",
				headers: {
					"Content-Type": "application/json",
					"Zotero-Allowed-Request": "1",
					"If-Unmodified-Since-Version": String(item.version || 0),
				},
				body: JSON.stringify({ collections: data.collections, tags: data.tags }),
				signal: AbortSignal.timeout(5000),
			});
			if (patch.ok) {
				Zotero.debug("[ZotPilot] local routing applied for " + itemKey);
			} else {
				Zotero.debug("[ZotPilot] local routing PATCH failed: " + patch.status);
			}
		} catch (e) {
			Zotero.debug("[ZotPilot] local routing error: " + e.message);
		}
	}

	async function _fetchRecentTopLevelItems() {
		let url = ZOTERO_LOCAL_API_URL
			+ "?format=json"
			+ "&limit=" + RECENT_ITEMS_LIMIT
			+ "&sort=dateAdded"
			+ "&direction=desc";
		try {
			let resp = await fetch(url, {
				method: "GET",
				headers: { "Accept": "application/json", "Zotero-Allowed-Request": "1" },
				signal: AbortSignal.timeout(LOCAL_API_TIMEOUT_MS),
			});
			if (!resp.ok) {
				Zotero.debug("[ZotPilot] local API recent-items lookup failed with status " + resp.status);
				return [];
			}
			let items = await resp.json();
			if (!Array.isArray(items)) {
				Zotero.debug("[ZotPilot] local API recent-items lookup returned non-array payload");
				return [];
			}
			return items.map(_extractRecentItem).filter(Boolean);
		} catch (e) {
			Zotero.debug("[ZotPilot] local API recent-items lookup failed: " + e.message);
			return [];
		}
	}

	/**
	 * Second handshake: poll Zotero local API until the newly saved item appears,
	 * confirming it has been written to the database. Returns item_key or null.
	 *
	 * Polls every 1s for up to 15s. Resolves as soon as a new item is detected
	 * that wasn't in the pre-save snapshot. This is event-driven in spirit —
	 * we stop as soon as we see the signal (new item), not after a fixed delay.
	 */
	async function _waitForItemInZotero(entry, tabTitle, beforeItems) {
		// Fast path: extension already captured item_key from itemProgress
		if (entry.item_key) return entry.item_key;

		const POLL_INTERVAL_MS = 1000;
		const MAX_ATTEMPTS = 15;
		const beforeKeys = new Set((beforeItems || []).map((item) => item.key).filter(Boolean));

		for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
			// First attempt: check immediately (progressWindow.done means Zotero is done,
			// item is usually already written). Subsequent attempts wait 1s between polls.
			if (attempt > 0) {
				await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
			}
			let afterItems = await _fetchRecentTopLevelItems();
			let newItems = afterItems.filter((item) => !beforeKeys.has(item.key));
			if (newItems.length === 1) {
				Zotero.debug("[ZotPilot] second handshake: found new item after " + attempt + " wait(s): " + newItems[0].key);
				return newItems[0].key;
			}
			if (newItems.length > 1) {
				// Multiple new items — try title match
				let matched = _pickBestMatchingItem(newItems, [entry.item_title, tabTitle]);
				if (matched) {
					Zotero.debug("[ZotPilot] second handshake: title-matched item after " + attempt + " wait(s): " + matched.key);
					return matched.key;
				}
				// Ambiguous — return first new item as best guess
				Zotero.debug("[ZotPilot] second handshake: ambiguous (" + newItems.length + " new items), using first");
				return newItems[0].key;
			}
			Zotero.debug("[ZotPilot] second handshake attempt " + (attempt + 1) + "/" + MAX_ATTEMPTS + ": no new item yet");
		}
		Zotero.debug("[ZotPilot] second handshake timed out — item_key unknown");
		return null;
	}

	async function _discoverItemKey(entry, tabTitle, beforeItems) {
		if (entry.item_key) return entry.item_key;

		let afterItems = await _fetchRecentTopLevelItems();
		if (!afterItems.length) return null;

		let beforeKeys = new Set((beforeItems || []).map((item) => item.key).filter(Boolean));
		let newItems = afterItems.filter((item) => !beforeKeys.has(item.key));

		if (newItems.length === 1) {
			return newItems[0].key;
		}

		let matched = _pickBestMatchingItem(newItems, [entry.item_title, tabTitle]);
		if (matched) {
			return matched.key;
		}

		if (!beforeKeys.size) {
			matched = _pickBestMatchingItem(afterItems, [entry.item_title, tabTitle]);
			if (matched) {
				return matched.key;
			}
		}

		if (newItems.length > 1) {
			Zotero.debug("[ZotPilot] item_key discovery ambiguous; multiple recent candidates remain");
		}
		return null;
	}

	/**
	 * Handle a save command.
	 *
	 * Flow:
	 *   1. Open tab (active:false)
	 *   2. Wait for page load + poll for translator detection
	 *   3. Set up completion promise via _pendingSaves Map
	 *   4. Call onZoteroButtonElementClick(tab) — public API, same as user click
	 *      Task 1.2: synchronous throw → save_trigger_failed (not a catch-all)
	 *   5. Wait for progressWindow.done → resolved by sendMessage patch (primary)
	 *      Task 1.2: 60s timeout → { success: "unconfirmed", error_code: "completion_unconfirmed" }
	 *   6. Post result with item_key/title (Task 1.3), detection telemetry (Task 1.2)
	 *   7. Close tab
	 */
	async function _handleSave(command) {
		const { request_id, url } = command;
		_busy = true;
		Zotero.Connector_Browser.setKeepServiceWorkerAlive(true);
		let tabId = null;

		try {
			// 1. Snapshot recent items BEFORE opening tab — ensures the new item
			//    appears in the post-save diff even if the translator fires quickly
			let recentItemsBeforeSave = await _fetchRecentTopLevelItems();

			// 2. Open tab
			let tab = await browser.tabs.create({ url: url, active: false });
			tabId = tab.id;

			// 3. Wait for page load + translator detection
			await _waitForReady(tab.id, 30000);

			// 4. Set up completion detection — keep local ref to entry so we can
			//    read item_key/item_title after the promise resolves (the Map entry
			//    is deleted by the patch that fires, but the object reference lives on)
			let entry = { resolve: null, item_key: null, item_title: null };
			let saveCompleted = new Promise((resolve) => {
				// Task 1.2: 60s timeout reports unconfirmed instead of false-positive success
				let timer = setTimeout(() => {
					_pendingSaves.delete(tab.id);
					Zotero.debug("[ZotPilot] save timeout (60s) — reporting unconfirmed");
					resolve({
						success: "unconfirmed",
						error_code: "completion_unconfirmed",
						error: "Save was triggered but no completion signal received within timeout. Check Zotero directly.",
						_via: "timeout",
					});
				}, SAVE_TIMEOUT_MS);
				entry.resolve = (result) => { clearTimeout(timer); resolve(result); };
				_pendingSaves.set(tab.id, entry);
			});

			// 5. Trigger save — Task 1.2: catch synchronous throw → save_trigger_failed.
			//    Do NOT fail early for missing translators — saveAsWebpage fallback handles those.
			tab = await browser.tabs.get(tab.id);
			// Capture tab title now (before tab closes) for use in _discoverItemKey
			let tabTitleAtSave = tab.title || "";

			// Anti-bot check: if the page title matches a known challenge pattern,
			// abort immediately without saving — avoids creating a junk Zotero item.
			// Keep the tab open so the user can complete the verification manually.
			if (ANTI_BOT_PATTERNS.some(p => tabTitleAtSave.toLowerCase().includes(p))) {
				_pendingSaves.delete(tab.id);
				tabId = null; // prevent finally from closing the tab
				await _postResult({
					request_id,
					success: false,
					error_code: "anti_bot_detected",
					error_message: `Anti-bot page detected (title: '${tabTitleAtSave}'). Please complete the verification in the Chrome tab that was left open, then retry.`,
					url,
					title: tabTitleAtSave,
				});
				return;
			}

			try {
				Zotero.Connector_Browser.onZoteroButtonElementClick(tab);
			} catch (err) {
				_pendingSaves.delete(tab.id);
				await _postResult({
					request_id,
					success: false,
					error_code: "save_trigger_failed",
					error_message: err.message || String(err),
					url,
				});
				return;
			}

			// 6. Wait for completion signal (progressWindow.done)
			let result = await saveCompleted;
			Zotero.debug("[ZotPilot] save result: success=" + result.success + " via=" + result._via);

			// Second handshake: after done signal, poll local API until the new item
			// actually appears in Zotero's database. This confirms the item was written
			// to SQLite — not just that the save was triggered. Prevents false positives
			// where progressWindow.done fires before Zotero finishes writing.
			// Also run for "unconfirmed" (60s timeout) — item may still have been saved.
			if (result.success === true || result.success === "unconfirmed") {
				entry.item_key = await _waitForItemInZotero(entry, tabTitleAtSave, recentItemsBeforeSave);
				Zotero.debug("[ZotPilot] second handshake item_key: " + entry.item_key);
				// If we found the item despite unconfirmed signal, upgrade to success
				if (result.success === "unconfirmed" && entry.item_key) {
					result = { ...result, success: true };
					Zotero.debug("[ZotPilot] unconfirmed upgraded to success — item found in Zotero");
				}
			}

			// 7a. Apply collection/tag routing via Zotero local API while item is guaranteed
			//     to exist in the local database — avoids cloud-sync race condition in bridge.
			if (entry.item_key && (command.collection_key || (command.tags && command.tags.length))) {
				await _applyLocalRouting(entry.item_key, command.collection_key, command.tags);
			}

			// 7. Post result — Task 1.3: include item_key and title for bridge-side routing
			await _postResult({
				request_id,
				success: result.success,
				...(result.error_code ? { error_code: result.error_code } : {}),
				...(result.error ? { error_message: result.error } : {}),
				url,
				title: entry.item_title || tab.title || "",
				item_key: entry.item_key || null,
				collection_key: command.collection_key || null,
				tags: command.tags || [],
				_detected_via: result._via,
			});

		} catch (err) {
			Zotero.logError(err);
			await _postResult({
				request_id,
				success: false,
				error_code: "save_trigger_failed",
				error_message: err.message || String(err),
				url,
			});
		} finally {
			// 8. Close tab and clean up
			if (tabId) {
				try { await browser.tabs.remove(tabId); } catch (e) {}
				_pendingSaves.delete(tabId);
			}
			Zotero.Connector_Browser.setKeepServiceWorkerAlive(false);
			_busy = false;
		}
	}

	async function _handlePreflight(command) {
		const { request_id, url } = command;
		_busy = true;
		Zotero.Connector_Browser.setKeepServiceWorkerAlive(true);
		let tabId = null;

		try {
			let tab = await browser.tabs.create({ url, active: false });
			tabId = tab.id;
			await _waitForReady(tab.id, 30000); // 30s matches _handleSave; 15s was too short for JS-redirect pages (AIP/APS)

			// Tab may have been closed by Chrome (e.g. PDF download triggered by URL).
			let title = "";
			let finalUrl = url;
			try {
				tab = await browser.tabs.get(tab.id);
				title = tab.title || "";
				finalUrl = tab.url || url;
			} catch (e) {
				// Tab already closed — PDF download completed, treat as accessible.
				await _postResult({ request_id, action: "preflight", status: "accessible", url, final_url: url, title: "" });
				tabId = null;
				return;
			}

			// Some publishers (AIP, APS) show a transient "请稍候…" / "Just a moment"
			// title during a JS redirect, then update it once the final page loads.
			// _waitForReady already waits for page stability, but title may lag behind
			// the URL change. If anti-bot pattern matches, wait up to 2×2s for title to
			// update before concluding it is a genuine challenge page.
			const MAX_TITLE_RETRIES = 2;
			const TITLE_RETRY_DELAY_MS = 2000;
			for (let i = 0; i < MAX_TITLE_RETRIES && ANTI_BOT_PATTERNS.some(p => title.toLowerCase().includes(p)); i++) {
				await new Promise(r => setTimeout(r, TITLE_RETRY_DELAY_MS));
				try {
					tab = await browser.tabs.get(tab.id);
					title = tab.title || "";
					finalUrl = tab.url || url;
				} catch (e) {
					// Tab closed during retry — treat as accessible.
					await _postResult({ request_id, action: "preflight", status: "accessible", url, final_url: url, title: "" });
					tabId = null;
					return;
				}
			}

			let status = "accessible";
			if (ANTI_BOT_PATTERNS.some((pattern) => title.toLowerCase().includes(pattern))) {
				status = "anti_bot_detected";
				tabId = null; // keep tab open — user needs it to complete verification
			}
			await _postResult({
				request_id,
				action: "preflight",
				status,
				url,
				final_url: finalUrl,
				title,
			});
		}
		catch (err) {
			await _postResult({
				request_id,
				action: "preflight",
				status: "error",
				url,
				error: err.message || String(err),
			});
		}
		finally {
			if (tabId) {
				try { await browser.tabs.remove(tabId); } catch (e) {}
			}
			Zotero.Connector_Browser.setKeepServiceWorkerAlive(false);
			_busy = false;
		}
	}

	/**
	 * Wait for tab to finish loading and for translators to be detected.
	 *
	 * Adaptive stability window: after status=complete, instead of immediately
	 * polling translators, wait for a short quiet period with no new tab events.
	 * If a URL change is observed (JS redirect), switch to a longer window and
	 * wait for the final page to settle before polling translators.
	 *
	 * - Normal pages (no redirect): STABILITY_WINDOW_MS quiet → poll translators
	 * - JS-redirect pages (URL change): STABILITY_WINDOW_REDIRECT_MS after last
	 *   event → poll translators on the final article page
	 *
	 * Reliability-first: values are conservative — better slow than wrong page.
	 */
	/**
	 * Wait for a tab to be ready for saving: page loaded, stable, and translator detected.
	 *
	 * Phase 1 — Stability: after status=complete, wait for a quiet period with no new
	 *   tab events (STABILITY_WINDOW_MS, or STABILITY_WINDOW_REDIRECT_MS after a JS redirect).
	 *   This lets multi-hop pages (AIP verify→article, Cloudflare→article) settle.
	 *
	 * Phase 2 — Translator readiness (event-driven, not polling):
	 *   After stability, check if translator is already registered. If yes, resolve immediately.
	 *   If not, register a waiter in _translatorWaiters — the onTranslators hook will resolve
	 *   it as soon as Zotero fires the event. A per-tab hard timeout cancels the waiter if
	 *   no translator arrives within TRANSLATOR_WAIT_MS.
	 *
	 * This replaces the old _pollForTranslators loop (fixed 5s), giving event-driven precision
	 * for fast pages and patient waiting for slow/redirect pages.
	 */
	const TRANSLATOR_WAIT_MS = 20000; // max wait for translator event after page stabilises

	function _waitForReady(tabId, timeout) {
		return new Promise((resolve) => {
			let resolved = false;
			let stabilityTimer = null;
			let redirectDetected = false;
			let translatorTimer = null;

			const hardTimer = setTimeout(() => {
				if (!resolved) {
					Zotero.debug("[ZotPilot] _waitForReady hard timeout for tab " + tabId);
					_cleanup();
					resolve();
				}
			}, timeout);

			function currentWindow() {
				return redirectDetected ? STABILITY_WINDOW_REDIRECT_MS : STABILITY_WINDOW_MS;
			}

			function _cleanup() {
				resolved = true;
				clearTimeout(hardTimer);
				if (stabilityTimer) clearTimeout(stabilityTimer);
				if (translatorTimer) clearTimeout(translatorTimer);
				_translatorWaiters.delete(tabId);
				browser.tabs.onUpdated.removeListener(onUpdated);
			}

			function _resolveNow() {
				if (!resolved) { _cleanup(); resolve(); }
			}

			function _waitForTranslatorEvent() {
				// Already registered by a prior redirect cycle? Clean up first.
				_translatorWaiters.delete(tabId);

				// Check if translator already available (fast path for pages that load quickly)
				let tabInfo = Zotero.Connector_Browser.getTabInfo(tabId);
				if (tabInfo && tabInfo.translators && tabInfo.translators.length > 0) {
					Zotero.debug("[ZotPilot] translator already ready for tab " + tabId);
					_resolveNow();
					return;
				}

				// Event-driven: wait for onTranslators hook to fire
				Zotero.debug("[ZotPilot] waiting for onTranslators event for tab " + tabId);
				_translatorWaiters.set(tabId, _resolveNow);

				// Timeout if translator never arrives (e.g. publisher has no Zotero translator)
				translatorTimer = setTimeout(() => {
					if (!resolved) {
						Zotero.debug("[ZotPilot] translator wait timeout for tab " + tabId + " — proceeding anyway");
						_translatorWaiters.delete(tabId);
						_resolveNow();
					}
				}, TRANSLATOR_WAIT_MS);
			}

			function scheduleStability() {
				if (resolved) return;
				if (stabilityTimer) clearTimeout(stabilityTimer);
				stabilityTimer = setTimeout(() => {
					if (!resolved) {
						browser.tabs.onUpdated.removeListener(onUpdated);
						_waitForTranslatorEvent();
					}
				}, currentWindow());
			}

			function onUpdated(id, changeInfo) {
				if (id !== tabId) return;
				if (changeInfo.url) {
					// URL change = JS redirect detected; reset translator waiter and use longer window
					redirectDetected = true;
					if (translatorTimer) { clearTimeout(translatorTimer); translatorTimer = null; }
					_translatorWaiters.delete(tabId);
				}
				if (changeInfo.status === "complete" || changeInfo.url) {
					scheduleStability();
				}
			}
			browser.tabs.onUpdated.addListener(onUpdated);

			// Fast path: tab already complete → start stability window immediately
			browser.tabs.get(tabId).then((tab) => {
				if (tab.status === "complete") {
					scheduleStability();
				}
			}).catch(() => {});
		});
	}

	/**
	 * Post result back to bridge.
	 */
	async function _postResult(result) {
		try {
			await fetch(BRIDGE_URL + "/result", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(result),
			});
		} catch (e) {
			Zotero.debug("AgentAPI: failed to post result: " + e.message);
		}
	}
};
