/**
 * ThemeAudioSettings - persistence and upgrade migration for the theme
 * background music settings.
 *
 * Split out of themeAudio.js because this is the one part of the audio
 * feature that has to reason about VALUES WRITTEN BY AN OLDER BUILD.
 * Everything else in the module only ever sees the current session's
 * state; this file is where an upgrade is allowed to change its mind
 * about what a stored string means.
 *
 * WHY A VERSION STAMP EXISTS AT ALL. Up to and including v0.7.2 the
 * master volume was an ATTENUATOR that defaulted to 0.3 and multiplied
 * with the per-theme manifest volume. Two individually sane numbers
 * compounded: 0.28 x 0.3 is a linear gain of 0.084, about -21.5 dB on
 * top of a -24 LUFS clip, which is inaudible on a phone. The fix made
 * the master UNITY and left the manifest as the only attenuation.
 *
 * That fix is invisible to anyone who already has a value under
 * `cloude.audio.volume`, because a stored value overrides the default by
 * construction - which is exactly the shape of upgrade bug that no test
 * against a fresh browser can ever see. So the migration below drops any
 * master volume written under the old budget and lets the new default
 * apply. Nothing is lost: no UI ever set that key deliberately, it was
 * only ever reachable from `ThemeAudio.setVolume()` in a console.
 *
 * WHY A SECOND VERSION STEP EXISTS. Up to and including the v2 schema
 * there was a SECOND gate in front of every per-session control: an app
 * sound master switch in the header kebab, persisted under
 * `cloude.audio.muted` and defaulting to OFF. Two controls, one silently
 * vetoing the other, was the whole bug. The switch is gone and audio is
 * session-only now.
 *
 * Deleting the switch without deleting the key would be strictly worse
 * than leaving it: every browser that ran a v2 build holds
 * `cloude.audio.muted = 'true'`, so a surviving reader would keep the app
 * permanently silent with no control left to undo it. v2 -> v3 therefore
 * DROPS the key, and nothing reads it any more.
 *
 * WHY A SECOND VOLUME KEY EXISTS. The v1 -> v2 step above DROPS
 * `cloude.audio.volume`, and it is right to: at v1 no UI could set that
 * key, so every value under it was written by `ThemeAudio.setVolume()`
 * from a console under the old attenuating budget, and keeping one
 * re-applied the inaudible gain. That reasoning stops being true the
 * moment a settings control exists. A future migration reading a bare
 * float cannot tell "console value from the bad budget" from "the number
 * the user deliberately dragged the slider to", and the safe-looking
 * choice - drop it - silently discards his choice.
 *
 * So a deliberate volume is NOT written to that key. It goes to
 * `cloude.audio.master` as a self-describing JSON object,
 * `{"v": 0.6, "setUnder": 3}`: a different key, a different shape, and a
 * record of the schema it was written under. A value there is proof a
 * build with a volume control wrote it, so no migration has grounds to
 * call it stale, and `setUnder` lets a future step reason about the
 * budget it was chosen against instead of guessing. The legacy key is
 * still READ as a fallback so an existing stamped store is not reset,
 * but it is never written again.
 *
 * THE FLOOR. `MIN_MASTER_VOLUME` is 0.35 and the slider cannot go below
 * it. A master of zero is silence with no error, which is the exact
 * failure this feature shipped six times; a floor makes the quietest
 * setting quiet rather than broken. 0.35 is derived, not picked: the
 * measured-inaudible case was 0.28 x 0.3 = 0.084 linear, and the quietest
 * manifest in the tree is 0.45, so the worst case here is 0.45 x 0.35 =
 * 0.158, about 5.5 dB above the level that was proven inaudible on a
 * phone. Values read out of storage are clamped up to the floor, so a 0
 * left behind by a console call heals on the next load instead of
 * persisting as silence.
 *
 * Keys owned here:
 *   cloude.audio.master          JSON {v: float, setUnder: int} - the
 *                                deliberate master gain. Never dropped by
 *                                a migration.
 *   cloude.audio.volume          float 0..1 as a string - LEGACY master
 *                                gain. Read as a fallback, never written.
 *   cloude.audio.settingsVersion integer as a string - schema stamp.
 *
 * Keys RETIRED here (removed by a migration, never read):
 *   cloude.audio.muted           the old app sound master switch.
 *
 * The per-session music opt-in (`cloude.audio.session.<tmux name>`) is
 * NOT owned here; it belongs to session-theme-menu.js, which is the
 * thing that knows what a session is.
 *
 * Every function takes the storage object explicitly rather than
 * reaching for `localStorage`, so the migration is unit-testable against
 * a plain object. Storage access is wrapped: Safari in private mode
 * throws on setItem, and an audio setting is never worth an exception.
 *
 * Exposed as window.ThemeAudioSettings. Loaded BEFORE themeAudio.js.
 */
(function () {
    'use strict';

    if (window.ThemeAudioSettings) {
        // Idempotent - never re-init on hot reload or a double script tag.
        return;
    }

    /**
     * RETIRED. The old app sound master switch, stored inverted as a mute
     * flag. Named only so the migration can delete it; no code reads it.
     */
    var LS_MUTED = 'cloude.audio.muted';
    /**
     * LEGACY master gain, 0..1, as a bare string. Read as a fallback for
     * a store stamped before the volume control existed; never written.
     */
    var LS_VOLUME = 'cloude.audio.volume';
    /**
     * The deliberate master gain, as JSON `{v: float, setUnder: int}`.
     * Written only by writeVolume(), which only the settings control and
     * ThemeAudio.setVolume() reach. Distinguishable from LS_VOLUME by key
     * AND by shape, which is what keeps a future migration from mistaking
     * a chosen value for a stale one.
     */
    var LS_MASTER = 'cloude.audio.master';
    /** Schema stamp, so an upgrade can reinterpret the keys above. */
    var LS_VERSION = 'cloude.audio.settingsVersion';

    /**
     * Current settings schema version.
     *
     * 1 (implicit, unstamped) - master volume is an attenuator, default 0.3.
     * 2                       - master volume is unity; the per-theme
     *                           manifest volume is the only attenuation.
     * 3                       - the app sound master switch is gone; audio
     *                           is session-only, so the stored mute flag
     *                           is deleted rather than left as a phantom
     *                           gate nothing can turn off.
     */
    var SETTINGS_VERSION = 3;

    /**
     * The master gain when nothing is stored. Unity on purpose: the
     * manifest volume is the only attenuation in the normal path, so the
     * two numbers cannot quietly multiply each other into silence.
     */
    var DEFAULT_MASTER_VOLUME = 1.0;

    /**
     * The lowest master gain the UI may select, and the floor every
     * stored value is clamped up to. See the file header for the
     * derivation: 0.45 (quietest manifest) x 0.35 = 0.158 linear, about
     * 5.5 dB above the 0.084 that was measured inaudible on a phone.
     * A slider that reaches zero is a mute in a costume.
     */
    var MIN_MASTER_VOLUME = 0.35;

    /**
     * Read a key, never throwing.
     *
     * @param {object} storage - a localStorage-like object.
     * @param {string} key - the key to read.
     * @returns {string|null} the stored string, or null if absent/unusable.
     */
    function _get(storage, key) {
        try {
            var v = storage.getItem(key);
            return v === undefined ? null : v;
        } catch (err) {
            return null;
        }
    }

    /**
     * Write a key, never throwing.
     *
     * @param {object} storage - a localStorage-like object.
     * @param {string} key - the key to write.
     * @param {string} value - the value to store.
     * @returns {void}
     */
    function _set(storage, key, value) {
        try {
            storage.setItem(key, String(value));
        } catch (err) {
            /* private mode, quota, or a storage-less environment */
        }
    }

    /**
     * Remove a key, never throwing. Falls back to writing the supplied
     * replacement when the storage object has no removeItem (some test
     * doubles and a few embedded webviews).
     *
     * @param {object} storage - a localStorage-like object.
     * @param {string} key - the key to drop.
     * @param {string} fallbackValue - written when removeItem is absent.
     * @returns {void}
     */
    function _remove(storage, key, fallbackValue) {
        try {
            if (typeof storage.removeItem === 'function') {
                storage.removeItem(key);
                return;
            }
        } catch (err) {
            /* fall through to the overwrite */
        }
        _set(storage, key, fallbackValue);
    }

    /**
     * The stored schema version. An unstamped store is version 1, which
     * is every browser that ran a build before this migration shipped.
     *
     * @param {object} storage - a localStorage-like object.
     * @returns {number} the stored version, or 1 when absent/unparseable.
     */
    function readVersion(storage) {
        var raw = _get(storage, LS_VERSION);
        if (raw === null) return 1;
        var n = parseInt(raw, 10);
        return isFinite(n) && n > 0 ? n : 1;
    }

    /**
     * Bring a store up to SETTINGS_VERSION. Idempotent: running it twice
     * is a no-op, and running it on a fresh browser only stamps.
     *
     * v1 -> v2 drops any stored master volume. See the file header: those
     * values were written under a gain budget where the master attenuated,
     * and keeping one silently re-applies the old inaudible gain on top of
     * the new manifest volumes.
     *
     * v2 -> v3 drops the retired app sound master switch. A stored 'true'
     * there was a gate that outranked every per-session control, and the
     * control that could clear it no longer exists.
     *
     * Both steps run on any store below the current version, so a v1 store
     * upgrades straight to v3 in one pass.
     *
     * @param {object} storage - a localStorage-like object.
     * @returns {{migrated: boolean, fromVersion: number,
     *            clearedVolume: string|null, clearedMute: string|null}}
     *          the cleared* fields hold the discarded strings, for logging,
     *          or null when there was nothing stored to discard.
     */
    function migrate(storage) {
        var from = readVersion(storage);
        if (from >= SETTINGS_VERSION) {
            return {
                migrated: false, fromVersion: from,
                clearedVolume: null, clearedMute: null
            };
        }

        var staleVolume = null;
        if (from < 2) {
            staleVolume = _get(storage, LS_VOLUME);
            if (staleVolume !== null) {
                _remove(storage, LS_VOLUME, String(DEFAULT_MASTER_VOLUME));
            }
        }

        var staleMute = null;
        if (from < 3) {
            staleMute = _get(storage, LS_MUTED);
            if (staleMute !== null) {
                _remove(storage, LS_MUTED, '');
            }
        }

        _set(storage, LS_VERSION, String(SETTINGS_VERSION));

        return {
            migrated: true, fromVersion: from,
            clearedVolume: staleVolume, clearedMute: staleMute
        };
    }

    /**
     * One log line describing what a migrate() result discarded. Lives
     * here rather than at the call site because only this module knows
     * what each cleared key meant.
     *
     * @param {object} m - a migrate() result.
     * @returns {string|null} the line, or null when nothing migrated.
     */
    function migrationSummary(m) {
        if (!m || !m.migrated) return null;
        return 'ThemeAudio: migrated settings v' + m.fromVersion +
            ' -> v' + SETTINGS_VERSION +
            (m.clearedVolume === null
                ? ''
                : ' (dropped stale master volume ' + m.clearedVolume + ')') +
            (m.clearedMute === null
                ? ''
                : ' (dropped retired app sound mute ' + m.clearedMute + ')');
    }

    /**
     * Clamp a gain into the usable band [MIN_MASTER_VOLUME, 1].
     *
     * Clamping UP is the point: a stored 0 is silence with no error, and
     * healing it on read is cheaper than explaining it later.
     *
     * @param {number} n - any number.
     * @returns {number} MIN_MASTER_VOLUME..1.
     */
    function clampVolume(n) {
        if (!isFinite(n)) return DEFAULT_MASTER_VOLUME;
        return Math.max(MIN_MASTER_VOLUME, Math.min(1, n));
    }

    /**
     * The deliberate master gain, or null when nothing usable is stored.
     * Rejects anything that is not the `{v: number}` shape this module
     * writes, so a hand-edited or half-written value falls through to the
     * legacy key rather than being trusted.
     *
     * @param {object} storage - a localStorage-like object.
     * @returns {number|null} the raw stored gain, unclamped, or null.
     */
    function _readMaster(storage) {
        var raw = _get(storage, LS_MASTER);
        if (raw === null) return null;
        var parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (err) {
            return null;
        }
        if (!parsed || typeof parsed !== 'object') return null;
        var n = parseFloat(parsed.v);
        if (!isFinite(n) || n < 0 || n > 1) return null;
        return n;
    }

    /**
     * The master gain, MIN_MASTER_VOLUME..1.
     *
     * Precedence: the deliberate value first, then the legacy key for a
     * store stamped before the control existed, then the default.
     * Out-of-range and unparseable values fall back rather than
     * attenuating by accident; in-range ones are clamped to the floor.
     *
     * @param {object} storage - a localStorage-like object.
     * @returns {number} MIN_MASTER_VOLUME..1.
     */
    function readVolume(storage) {
        var chosen = _readMaster(storage);
        if (chosen !== null) return clampVolume(chosen);

        var raw = _get(storage, LS_VOLUME);
        if (raw === null) return DEFAULT_MASTER_VOLUME;
        var n = parseFloat(raw);
        if (isFinite(n) && n >= 0 && n <= 1) return clampVolume(n);
        return DEFAULT_MASTER_VOLUME;
    }

    /**
     * Persist the master gain as a deliberate choice.
     *
     * Writes the new self-describing key ONLY. The legacy bare-float key
     * is left exactly as it is: it is the one a migration is allowed to
     * drop, and nothing the user chose may ever live there again.
     *
     * @param {object} storage - a localStorage-like object.
     * @param {number} v - 0..1; stored clamped to the usable band.
     * @returns {void}
     */
    function writeVolume(storage, v) {
        _set(storage, LS_MASTER, JSON.stringify({
            v: clampVolume(parseFloat(v)),
            setUnder: SETTINGS_VERSION
        }));
    }

    window.ThemeAudioSettings = {
        LS_MUTED: LS_MUTED,
        LS_VOLUME: LS_VOLUME,
        LS_VERSION: LS_VERSION,
        SETTINGS_VERSION: SETTINGS_VERSION,
        LS_MASTER: LS_MASTER,
        DEFAULT_MASTER_VOLUME: DEFAULT_MASTER_VOLUME,
        MIN_MASTER_VOLUME: MIN_MASTER_VOLUME,
        clampVolume: clampVolume,
        readVersion: readVersion,
        migrate: migrate,
        migrationSummary: migrationSummary,
        readVolume: readVolume,
        writeVolume: writeVolume
    };
})();
