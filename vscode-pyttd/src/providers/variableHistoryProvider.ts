import * as vscode from 'vscode';
import * as crypto from 'crypto';

export class VariableHistoryProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'pyttd.variableHistory';

    private _view?: vscode.WebviewView;
    private readonly _extensionUri: vscode.Uri;

    constructor(extensionUri: vscode.Uri) {
        this._extensionUri = extensionUri;
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): void {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._extensionUri, 'media'),
            ],
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        webviewView.webview.onDidReceiveMessage((message) => {
            if (message.type === 'navigateToSeq') {
                const session = vscode.debug.activeDebugSession;
                if (session && session.type === 'pyttd') {
                    session.customRequest('goto_frame', { targetSeq: message.seq });
                }
            }
        });
    }

    public showHistory(variableName: string, history: any[]): void {
        this._view?.webview.postMessage({
            type: 'showHistory',
            variableName,
            history,
        });
    }

    private _getHtmlForWebview(webview: vscode.Webview): string {
        const nonce = crypto.randomBytes(16).toString('hex');

        const cssUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'variableHistory.css')
        );
        const jsUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'variableHistory.js')
        );

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="${cssUri}" rel="stylesheet">
    <title>Variable History</title>
</head>
<body>
    <div id="container">
        <div id="placeholder">Select a variable to view its history</div>
        <div id="header" style="display:none">
            <span id="var-name"></span>
            <span id="var-count"></span>
        </div>
        <canvas id="chart-canvas" style="display:none"></canvas>
        <div id="table-container" style="display:none">
            <table id="history-table">
                <thead><tr><th>Seq</th><th>Value</th><th>Location</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>
    </div>
    <div id="tooltip" class="tooltip"></div>
    <script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
    }
}
