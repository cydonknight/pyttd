import * as assert from 'assert';
import { installMock, MockUri } from './mock-vscode';

// Install vscode mock BEFORE importing provider modules
installMock();

import { VariableHistoryProvider } from '../providers/variableHistoryProvider';

describe('VariableHistoryProvider', () => {
    let provider: VariableHistoryProvider;

    beforeEach(() => {
        provider = new VariableHistoryProvider(new MockUri('/test/extension') as any);
    });

    describe('resolveWebviewView', () => {
        it('should set HTML content on the webview', () => {
            let assignedHtml = '';
            const mockWebviewView = {
                webview: {
                    options: {} as any,
                    html: '',
                    asWebviewUri: (uri: any) => ({ toString: () => uri.fsPath || String(uri) }),
                    cspSource: 'test-csp',
                    onDidReceiveMessage: () => ({ dispose: () => {} }),
                },
            };
            Object.defineProperty(mockWebviewView.webview, 'html', {
                set(val: string) { assignedHtml = val; },
                get() { return assignedHtml; },
            });

            provider.resolveWebviewView(
                mockWebviewView as any,
                {} as any,
                { isCancellationRequested: false, onCancellationRequested: () => ({ dispose: () => {} }) } as any
            );

            assert.ok(assignedHtml.includes('<!DOCTYPE html>'));
            assert.ok(assignedHtml.includes('variableHistory.css'));
            assert.ok(assignedHtml.includes('variableHistory.js'));
            assert.ok(assignedHtml.includes('Content-Security-Policy'));
            assert.ok(assignedHtml.includes('chart-canvas'));
            assert.ok(assignedHtml.includes('history-table'));
        });
    });

    describe('showHistory', () => {
        it('should post message to webview when view is resolved', () => {
            const messages: any[] = [];
            const mockWebviewView = {
                webview: {
                    options: {} as any,
                    html: '',
                    asWebviewUri: (uri: any) => uri,
                    cspSource: 'test-csp',
                    onDidReceiveMessage: () => ({ dispose: () => {} }),
                    postMessage: (msg: any) => { messages.push(msg); return Promise.resolve(true); },
                },
            };

            provider.resolveWebviewView(
                mockWebviewView as any,
                {} as any,
                { isCancellationRequested: false, onCancellationRequested: () => ({ dispose: () => {} }) } as any
            );

            const testHistory = [
                { seq: 10, value: '1', line: 5, filename: 'test.py', functionName: 'foo' },
                { seq: 20, value: '2', line: 6, filename: 'test.py', functionName: 'foo' },
            ];
            provider.showHistory('x', testHistory);

            assert.strictEqual(messages.length, 1);
            assert.strictEqual(messages[0].type, 'showHistory');
            assert.strictEqual(messages[0].variableName, 'x');
            assert.deepStrictEqual(messages[0].history, testHistory);
        });

        it('should not throw when view is not resolved', () => {
            // showHistory before resolveWebviewView — should be a no-op
            assert.doesNotThrow(() => {
                provider.showHistory('y', []);
            });
        });
    });

    describe('message handling', () => {
        it('should handle navigateToSeq messages', () => {
            let messageHandler: ((msg: any) => void) | null = null;
            const mockWebviewView = {
                webview: {
                    options: {} as any,
                    html: '',
                    asWebviewUri: (uri: any) => uri,
                    cspSource: 'test-csp',
                    onDidReceiveMessage: (handler: (msg: any) => void) => {
                        messageHandler = handler;
                        return { dispose: () => {} };
                    },
                },
            };

            provider.resolveWebviewView(
                mockWebviewView as any,
                {} as any,
                { isCancellationRequested: false, onCancellationRequested: () => ({ dispose: () => {} }) } as any
            );

            // The handler is registered; verify it was captured
            assert.ok(messageHandler !== null, 'message handler should be registered');
        });
    });
});
