(function () {
    'use strict';

    const HTML_ENTITIES = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    };

    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(
            /[&<>"']/g,
            character => HTML_ENTITIES[character]
        );
    }

    function safeUrl(value, allowRelative = true) {
        if (typeof value !== 'string' || !value.trim()) return null;
        const candidate = value.trim();
        try {
            const parsed = new URL(candidate, window.location.origin);
            if (!['http:', 'https:'].includes(parsed.protocol)) return null;
            if (allowRelative && parsed.origin === window.location.origin) {
                return `${parsed.pathname}${parsed.search}${parsed.hash}`;
            }
            return parsed.href;
        } catch {
            return null;
        }
    }

    function safeColor(value, fallback = '#666666') {
        return typeof value === 'string' &&
            /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i.test(value)
            ? value
            : fallback;
    }

    function classToken(value, fallback = 'unknown') {
        const token = String(value == null ? '' : value).toLowerCase();
        return /^[a-z0-9_-]+$/.test(token) ? token : fallback;
    }

    function cssEscape(value) {
        const text = String(value == null ? '' : value);
        if (window.CSS && typeof window.CSS.escape === 'function') {
            return window.CSS.escape(text);
        }
        return text.replace(/[\0-\x1f\x7f"\\]/g, character => {
            return `\\${character.codePointAt(0).toString(16)} `;
        });
    }

    function finiteNumber(value, fallback = 0) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function appendTimestampedLog(container, message, type = 'info', timestampValue = null) {
        if (!container) return null;
        const allowedTypes = new Set([
            'info', 'primary', 'secondary', 'success', 'warning', 'danger',
            'muted'
        ]);
        const normalizedType = allowedTypes.has(type) ? type : 'info';
        const normalizedMessage = String(message ?? '');
        const normalizedTimestamp = timestampValue || new Date().toLocaleTimeString();
        const entry = document.createElement('div');
        entry.className = `mb-1 text-${normalizedType}`;
        entry.dataset.logMessage = normalizedMessage;
        entry.dataset.logType = normalizedType;
        entry.dataset.logTimestamp = normalizedTimestamp;

        const timestamp = document.createElement('span');
        timestamp.className = 'text-muted';
        timestamp.textContent = `[${normalizedTimestamp}]`;
        entry.append(timestamp, document.createTextNode(` ${normalizedMessage}`));
        container.appendChild(entry);
        return entry;
    }

    window.SafeDOM = Object.freeze({
        escapeHtml,
        escapeAttribute: escapeHtml,
        safeUrl,
        safeColor,
        classToken,
        cssEscape,
        finiteNumber,
        appendTimestampedLog
    });
})();
