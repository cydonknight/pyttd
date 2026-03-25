// Variable History Webview
// Chart/table visualization for variable value over time

(function () {
    'use strict';

    const vscode = acquireVsCodeApi();
    const canvas = document.getElementById('chart-canvas');
    const tooltip = document.getElementById('tooltip');
    const placeholder = document.getElementById('placeholder');
    const header = document.getElementById('header');
    const varNameEl = document.getElementById('var-name');
    const varCountEl = document.getElementById('var-count');
    const tableContainer = document.getElementById('table-container');
    const tableBody = document.querySelector('#history-table tbody');
    const ctx = canvas ? canvas.getContext('2d') : null;

    let history = [];
    let variableName = '';
    let isNumericMode = false;

    // --- Colors (VSCode theme-aware) ---
    function getColors() {
        const style = getComputedStyle(document.body);
        return {
            bg: style.getPropertyValue('--vscode-editor-background').trim() || '#1e1e1e',
            fg: style.getPropertyValue('--vscode-editor-foreground').trim() || '#cccccc',
            line: style.getPropertyValue('--vscode-charts-blue').trim() || '#3794ff',
            point: style.getPropertyValue('--vscode-charts-green').trim() || '#89d185',
            grid: style.getPropertyValue('--vscode-editorWidget-border').trim() || '#454545',
        };
    }

    // --- Numeric detection ---
    function isAllNumeric(entries) {
        if (entries.length === 0) return false;
        return entries.every(function (e) {
            var v = e.value;
            if (typeof v === 'number') return true;
            if (typeof v === 'string') {
                var n = parseFloat(v);
                return !isNaN(n) && isFinite(n);
            }
            return false;
        });
    }

    function numVal(v) {
        return typeof v === 'number' ? v : parseFloat(v);
    }

    // --- Chart rendering ---
    function drawChart() {
        if (!canvas || !ctx || history.length === 0) return;

        var dpr = window.devicePixelRatio || 1;
        var rect = canvas.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        ctx.scale(dpr, dpr);
        var w = rect.width;
        var h = rect.height;

        var colors = getColors();
        ctx.fillStyle = colors.bg;
        ctx.fillRect(0, 0, w, h);

        var padding = { top: 10, right: 10, bottom: 20, left: 50 };
        var plotW = w - padding.left - padding.right;
        var plotH = h - padding.top - padding.bottom;

        if (plotW <= 0 || plotH <= 0) return;

        // Compute ranges
        var vals = history.map(function (e) { return numVal(e.value); });
        var minVal = Math.min.apply(null, vals);
        var maxVal = Math.max.apply(null, vals);
        if (minVal === maxVal) {
            minVal -= 1;
            maxVal += 1;
        }
        var minSeq = history[0].seq;
        var maxSeq = history[history.length - 1].seq;
        if (minSeq === maxSeq) {
            minSeq -= 1;
            maxSeq += 1;
        }

        function xPos(seq) {
            return padding.left + ((seq - minSeq) / (maxSeq - minSeq)) * plotW;
        }
        function yPos(val) {
            return padding.top + plotH - ((val - minVal) / (maxVal - minVal)) * plotH;
        }

        // Grid lines
        ctx.strokeStyle = colors.grid;
        ctx.lineWidth = 0.5;
        for (var i = 0; i <= 4; i++) {
            var gy = padding.top + (i / 4) * plotH;
            ctx.beginPath();
            ctx.moveTo(padding.left, gy);
            ctx.lineTo(w - padding.right, gy);
            ctx.stroke();
        }

        // Y-axis labels
        ctx.fillStyle = colors.fg;
        ctx.font = '10px ' + (getComputedStyle(document.body).getPropertyValue('--vscode-font-family') || 'monospace');
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (var j = 0; j <= 4; j++) {
            var labelVal = maxVal - (j / 4) * (maxVal - minVal);
            var labelY = padding.top + (j / 4) * plotH;
            var label = labelVal % 1 === 0 ? labelVal.toString() : labelVal.toFixed(2);
            if (label.length > 7) label = labelVal.toExponential(1);
            ctx.fillText(label, padding.left - 4, labelY);
        }

        // Data line
        ctx.strokeStyle = colors.line;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (var k = 0; k < history.length; k++) {
            var px = xPos(history[k].seq);
            var py = yPos(numVal(history[k].value));
            if (k === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
        }
        ctx.stroke();

        // Data points
        ctx.fillStyle = colors.point;
        for (var m = 0; m < history.length; m++) {
            var dx = xPos(history[m].seq);
            var dy = yPos(numVal(history[m].value));
            ctx.beginPath();
            ctx.arc(dx, dy, 3, 0, Math.PI * 2);
            ctx.fill();
        }

        // X-axis labels (first and last seq)
        ctx.fillStyle = colors.fg;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(minSeq.toString(), padding.left, h - padding.bottom + 4);
        ctx.fillText(maxSeq.toString(), w - padding.right, h - padding.bottom + 4);
    }

    // --- Chart interaction ---
    function findNearestPoint(clientX) {
        if (!canvas || history.length === 0) return -1;
        var rect = canvas.getBoundingClientRect();
        var x = clientX - rect.left;
        var padding = { left: 50, right: 10 };
        var plotW = rect.width - padding.left - padding.right;
        var minSeq = history[0].seq;
        var maxSeq = history[history.length - 1].seq;
        if (maxSeq === minSeq) return 0;

        var ratio = (x - padding.left) / plotW;
        var targetSeq = minSeq + ratio * (maxSeq - minSeq);
        var best = 0;
        var bestDist = Math.abs(history[0].seq - targetSeq);
        for (var i = 1; i < history.length; i++) {
            var dist = Math.abs(history[i].seq - targetSeq);
            if (dist < bestDist) {
                bestDist = dist;
                best = i;
            }
        }
        return best;
    }

    if (canvas) {
        canvas.addEventListener('click', function (e) {
            var idx = findNearestPoint(e.clientX);
            if (idx >= 0 && idx < history.length) {
                vscode.postMessage({ type: 'navigateToSeq', seq: history[idx].seq });
            }
        });

        canvas.addEventListener('mousemove', function (e) {
            var idx = findNearestPoint(e.clientX);
            if (idx >= 0 && idx < history.length) {
                var entry = history[idx];
                tooltip.textContent = 'seq ' + entry.seq + ': ' + entry.value +
                    ' (' + (entry.filename || '').split('/').pop() + ':' + entry.line + ')';
                tooltip.style.display = 'block';
                tooltip.style.left = (e.clientX - canvas.getBoundingClientRect().left + 10) + 'px';
                tooltip.style.top = (e.clientY - canvas.getBoundingClientRect().top - 25) + 'px';
            } else {
                tooltip.style.display = 'none';
            }
        });

        canvas.addEventListener('mouseleave', function () {
            tooltip.style.display = 'none';
        });
    }

    // --- Table rendering ---
    function renderTable() {
        if (!tableBody) return;
        tableBody.innerHTML = '';
        for (var i = 0; i < history.length; i++) {
            var entry = history[i];
            var tr = document.createElement('tr');
            tr.dataset.seq = entry.seq;
            tr.innerHTML =
                '<td>' + entry.seq + '</td>' +
                '<td title="' + escapeHtml(String(entry.value)) + '">' + escapeHtml(String(entry.value)) + '</td>' +
                '<td>' + escapeHtml((entry.filename || '').split('/').pop() + ':' + entry.line) + '</td>';
            tr.addEventListener('click', (function (seq) {
                return function () {
                    vscode.postMessage({ type: 'navigateToSeq', seq: seq });
                };
            })(entry.seq));
            tableBody.appendChild(tr);
        }
    }

    function escapeHtml(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // --- Message handler ---
    window.addEventListener('message', function (event) {
        var msg = event.data;
        if (msg.type === 'showHistory') {
            variableName = msg.variableName || '';
            history = msg.history || [];

            placeholder.style.display = 'none';
            header.style.display = 'block';
            varNameEl.textContent = variableName;
            varCountEl.textContent = '(' + history.length + ' change' + (history.length !== 1 ? 's' : '') + ')';

            isNumericMode = isAllNumeric(history);

            if (isNumericMode && history.length >= 2) {
                canvas.style.display = 'block';
                tableContainer.style.display = 'none';
                drawChart();
            } else {
                canvas.style.display = 'none';
                tableContainer.style.display = 'block';
                renderTable();
            }
        }
    });

    // Handle resize
    if (canvas) {
        var resizeObserver = new ResizeObserver(function () {
            if (isNumericMode && history.length >= 2) {
                drawChart();
            }
        });
        resizeObserver.observe(canvas.parentElement);
    }
})();
