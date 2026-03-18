import * as vscode from 'vscode';
import * as crypto from 'crypto';

export class TimelineScrubberProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'pyttd.timeline';

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
            this._handleMessage(message);
        });
    }

    public postMessage(message: any): void {
        this._view?.webview.postMessage(message);
    }

    private _handleMessage(message: any): void {
        const session = vscode.debug.activeDebugSession;
        if (!session || session.type !== 'pyttd') {
            return;
        }

        switch (message.type) {
            case 'scrub':
                session.customRequest('goto', { threadId: 1, targetId: message.seq });
                break;
            case 'stepBack':
                session.customRequest('stepBack', { threadId: 1 });
                break;
            case 'stepForward':
                session.customRequest('next', { threadId: 1 });
                break;
            case 'gotoFirst':
                session.customRequest('goto', { threadId: 1, targetId: 0 });
                break;
            case 'gotoLast':
                if (message.totalFrames != null) {
                    session.customRequest('goto', { threadId: 1, targetId: message.totalFrames });
                }
                break;
            case 'zoom':
                session.customRequest('get_timeline_summary', {
                    startSeq: message.startSeq,
                    endSeq: message.endSeq,
                    bucketCount: 500,
                });
                break;
        }
    }

    private _getHtmlForWebview(webview: vscode.Webview): string {
        const nonce = crypto.randomBytes(16).toString('hex');

        const cssUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'timelineScrubber.css')
        );
        const jsUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'timelineScrubber.js')
        );

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="${cssUri}" rel="stylesheet">
    <title>Timeline</title>
</head>
<body>
    <div id="timeline-container">
        <canvas id="timeline-canvas"></canvas>
        <div id="tooltip" class="tooltip"></div>
    </div>
    <script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
    }
}
