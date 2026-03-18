// Timeline Scrubber Webview
// Canvas-based timeline visualization for pyttd time-travel debugger

(function () {
    'use strict';

    const vscode = acquireVsCodeApi();
    const canvas = document.getElementById('timeline-canvas');
    const tooltip = document.getElementById('tooltip');
    const ctx = canvas.getContext('2d');

    // State
    let buckets = [];
    let totalFrames = 0;
    let viewStartSeq = 0;
    let viewEndSeq = 0;
    let currentSeq = -1;
    let maxDepthOverall = 1;
    let isDragging = false;
    let pendingGoto = null;
    let lastGotoTime = 0;
    const GOTO_THROTTLE_MS = 150;

    // Zoom cache: keyed by "startSeq:endSeq:bucketCount"
    const zoomCache = new Map();
    const MAX_CACHE_ENTRIES = 4;

    // --- Colors (VSCode theme-aware) ---
    function getColors() {
        const style = getComputedStyle(document.body);
        return {
            bg: style.getPropertyValue('--vscode-editor-background').trim() || '#1e1e1e',
            fg: style.getPropertyValue('--vscode-editor-foreground').trim() || '#cccccc',
            bar: style.getPropertyValue('--vscode-charts-blue').trim() || '#3794ff',
            exception: style.getPropertyValue('--vscode-charts-red').trim() || '#f14c4c',
            breakpoint: style.getPropertyValue('--vscode-charts-orange').trim() || '#cca700',
            cursor: style.getPropertyValue('--vscode-charts-yellow').trim() || '#e2e210',
            gridLine: style.getPropertyValue('--vscode-editorWidget-border').trim() || '#454545',
        };
    }

    // --- Rendering ---
    function render() {
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.parentElement.getBoundingClientRect();
        const w = rect.width;
        const h = rect.height;

        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        const colors = getColors();
        const padding = { top: 4, bottom: 16, left: 2, right: 2 };
        const drawW = w - padding.left - padding.right;
        const drawH = h - padding.top - padding.bottom;

        // Clear
        ctx.fillStyle = colors.bg;
        ctx.fillRect(0, 0, w, h);

        if (buckets.length === 0 || drawW <= 0 || drawH <= 0) {
            ctx.fillStyle = colors.fg;
            ctx.font = '11px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No timeline data', w / 2, h / 2);
            return;
        }

        // Compute max depth for scaling
        maxDepthOverall = 1;
        for (const b of buckets) {
            if (b.maxCallDepth > maxDepthOverall) {
                maxDepthOverall = b.maxCallDepth;
            }
        }

        const barW = Math.max(1, drawW / buckets.length);
        const depthScale = drawH / (maxDepthOverall + 1);

        // Draw bars
        for (let i = 0; i < buckets.length; i++) {
            const b = buckets[i];
            const x = padding.left + i * barW;
            const barH = Math.max(2, (b.maxCallDepth + 1) * depthScale);
            const y = padding.top + drawH - barH;

            // Bar color: red for exceptions, orange for breakpoints, blue otherwise
            if (b.hasException) {
                ctx.fillStyle = colors.exception;
            } else if (b.hasBreakpoint) {
                ctx.fillStyle = colors.breakpoint;
            } else {
                ctx.fillStyle = colors.bar;
            }

            ctx.fillRect(x, y, Math.max(1, barW - (barW > 3 ? 1 : 0)), barH);
        }

        // Draw current position cursor
        if (currentSeq >= 0 && viewEndSeq > viewStartSeq) {
            const range = viewEndSeq - viewStartSeq;
            const cursorX = padding.left + ((currentSeq - viewStartSeq) / range) * drawW;
            if (cursorX >= padding.left && cursorX <= padding.left + drawW) {
                ctx.strokeStyle = colors.cursor;
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(cursorX, padding.top);
                ctx.lineTo(cursorX, padding.top + drawH);
                ctx.stroke();

                // Small triangle at bottom
                ctx.fillStyle = colors.cursor;
                ctx.beginPath();
                ctx.moveTo(cursorX - 4, padding.top + drawH + 2);
                ctx.lineTo(cursorX + 4, padding.top + drawH + 2);
                ctx.lineTo(cursorX, padding.top + drawH + 8);
                ctx.closePath();
                ctx.fill();
            }
        }

        // Sequence range labels
        ctx.fillStyle = colors.fg;
        ctx.font = '9px sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(String(viewStartSeq), padding.left, h - 2);
        ctx.textAlign = 'right';
        ctx.fillText(String(viewEndSeq), w - padding.right, h - 2);
    }

    // --- Interaction helpers ---
    function seqFromX(clientX) {
        const rect = canvas.getBoundingClientRect();
        const padding = { left: 2, right: 2 };
        const drawW = rect.width - padding.left - padding.right;
        const x = clientX - rect.left - padding.left;
        const ratio = Math.max(0, Math.min(1, x / drawW));
        return Math.round(viewStartSeq + ratio * (viewEndSeq - viewStartSeq));
    }

    function bucketFromX(clientX) {
        const rect = canvas.getBoundingClientRect();
        const padding = { left: 2, right: 2 };
        const drawW = rect.width - padding.left - padding.right;
        const x = clientX - rect.left - padding.left;
        const idx = Math.floor((x / drawW) * buckets.length);
        return buckets[Math.max(0, Math.min(buckets.length - 1, idx))];
    }

    function throttledGoto(seq) {
        const now = Date.now();
        if (now - lastGotoTime >= GOTO_THROTTLE_MS) {
            lastGotoTime = now;
            vscode.postMessage({ type: 'scrub', seq: seq });
            pendingGoto = null;
        } else {
            pendingGoto = seq;
            setTimeout(function () {
                if (pendingGoto !== null) {
                    lastGotoTime = Date.now();
                    vscode.postMessage({ type: 'scrub', seq: pendingGoto });
                    pendingGoto = null;
                }
            }, GOTO_THROTTLE_MS - (now - lastGotoTime));
        }
    }

    // --- Mouse events ---
    canvas.addEventListener('mousedown', function (e) {
        if (e.button !== 0 || buckets.length === 0) return;
        isDragging = true;
        const seq = seqFromX(e.clientX);
        currentSeq = seq;
        render();
    });

    canvas.addEventListener('mousemove', function (e) {
        if (isDragging) {
            const seq = seqFromX(e.clientX);
            currentSeq = seq;
            render();
            throttledGoto(seq);
        } else if (buckets.length > 0) {
            // Show tooltip
            const b = bucketFromX(e.clientX);
            if (b) {
                tooltip.textContent = b.dominantFunction +
                    ' (seq ' + b.startSeq + '-' + b.endSeq +
                    ', depth ' + b.maxCallDepth + ')';
                tooltip.style.display = 'block';
                const rect = canvas.getBoundingClientRect();
                tooltip.style.left = (e.clientX - rect.left + 10) + 'px';
                tooltip.style.top = (e.clientY - rect.top - 24) + 'px';
            }
        }
    });

    canvas.addEventListener('mouseup', function (e) {
        if (!isDragging) return;
        isDragging = false;
        pendingGoto = null; // Cancel any throttled goto from drag
        const seq = seqFromX(e.clientX);
        currentSeq = seq;
        render();
        vscode.postMessage({ type: 'scrub', seq: seq });
    });

    canvas.addEventListener('mouseleave', function () {
        if (isDragging) {
            pendingGoto = null;
        }
        isDragging = false;
        tooltip.style.display = 'none';
    });

    // --- Mousewheel zoom ---
    canvas.addEventListener('wheel', function (e) {
        e.preventDefault();
        if (totalFrames <= 0) return;

        const range = viewEndSeq - viewStartSeq;
        if (range <= 0) return;

        // Zoom center based on mouse position
        const seq = seqFromX(e.clientX);
        const zoomFactor = e.deltaY > 0 ? 1.3 : 0.7;
        const newRange = Math.max(10, Math.min(totalFrames, Math.round(range * zoomFactor)));

        // Center on mouse position
        const ratio = (seq - viewStartSeq) / range;
        let newStart = Math.round(seq - ratio * newRange);
        let newEnd = newStart + newRange;

        // Clamp
        if (newStart < 0) { newEnd -= newStart; newStart = 0; }
        if (newEnd > totalFrames) { newEnd = totalFrames; newStart = Math.max(0, newEnd - newRange); }

        // Check cache
        const cacheKey = newStart + ':' + newEnd + ':500';
        const cached = zoomCache.get(cacheKey);
        if (cached) {
            viewStartSeq = newStart;
            viewEndSeq = newEnd;
            buckets = cached;
            render();
        } else {
            // Request new data
            viewStartSeq = newStart;
            viewEndSeq = newEnd;
            vscode.postMessage({ type: 'zoom', startSeq: newStart, endSeq: newEnd });
        }
    }, { passive: false });

    // --- Keyboard ---
    document.addEventListener('keydown', function (e) {
        switch (e.key) {
            case 'ArrowLeft':
                e.preventDefault();
                vscode.postMessage({ type: 'stepBack' });
                break;
            case 'ArrowRight':
                e.preventDefault();
                vscode.postMessage({ type: 'stepForward' });
                break;
            case 'Home':
                e.preventDefault();
                vscode.postMessage({ type: 'gotoFirst' });
                break;
            case 'End':
                e.preventDefault();
                if (totalFrames > 0) {
                    vscode.postMessage({ type: 'gotoLast', totalFrames: totalFrames - 1 });
                }
                break;
            case 'PageUp':
                e.preventDefault();
                if (totalFrames > 0) {
                    const jump = Math.max(1, Math.round(totalFrames * 0.1));
                    const target = Math.max(0, currentSeq - jump);
                    vscode.postMessage({ type: 'scrub', seq: target });
                }
                break;
            case 'PageDown':
                e.preventDefault();
                if (totalFrames > 0) {
                    const jump = Math.max(1, Math.round(totalFrames * 0.1));
                    const target = Math.min(totalFrames, currentSeq + jump);
                    vscode.postMessage({ type: 'scrub', seq: target });
                }
                break;
        }
    });

    // --- Message handling from extension ---
    window.addEventListener('message', function (event) {
        const msg = event.data;
        if (!msg) return;

        if (msg.type === 'pyttd/timelineData') {
            const data = msg.data;
            buckets = data.buckets || [];
            totalFrames = data.totalFrames || 0;
            viewStartSeq = data.startSeq != null ? data.startSeq : 0;
            viewEndSeq = data.endSeq != null ? data.endSeq : totalFrames;

            // Cache for zoom
            const cacheKey = viewStartSeq + ':' + viewEndSeq + ':500';
            zoomCache.set(cacheKey, buckets);
            // Evict old cache entries
            if (zoomCache.size > MAX_CACHE_ENTRIES) {
                const firstKey = zoomCache.keys().next().value;
                zoomCache.delete(firstKey);
            }

            render();
        } else if (msg.type === 'pyttd/positionChanged') {
            const data = msg.data;
            if (data.seq != null) {
                currentSeq = data.seq;
                render();
            }
        }
    });

    // --- Resize handling ---
    const resizeObserver = new ResizeObserver(function () {
        render();
    });
    resizeObserver.observe(canvas.parentElement);

    // Initial render
    render();
})();
