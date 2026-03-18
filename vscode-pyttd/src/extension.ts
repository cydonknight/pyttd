import * as vscode from 'vscode';
import { PyttdDebugSession } from './debugAdapter/pyttdDebugSession';
import { TimelineScrubberProvider } from './providers/timelineScrubberProvider';
import { PyttdCodeLensProvider } from './providers/codeLensProvider';
import { PyttdInlineValuesProvider } from './providers/inlineValuesProvider';
import { PyttdCallHistoryProvider } from './providers/callHistoryProvider';

export function activate(context: vscode.ExtensionContext) {
    context.subscriptions.push(
        vscode.debug.registerDebugAdapterDescriptorFactory('pyttd', {
            createDebugAdapterDescriptor(_session: vscode.DebugSession): vscode.ProviderResult<vscode.DebugAdapterDescriptor> {
                return new vscode.DebugAdapterInlineImplementation(new PyttdDebugSession());
            }
        })
    );

    // Register timeline webview provider
    const timelineProvider = new TimelineScrubberProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('pyttd.timeline', timelineProvider)
    );

    // Phase 6: CodeLens, Inline Values, Call History
    const codeLensProvider = new PyttdCodeLensProvider();
    const callHistoryProvider = new PyttdCallHistoryProvider();
    let callHistoryRefreshed = false;

    context.subscriptions.push(
        vscode.languages.registerCodeLensProvider({ language: 'python' }, codeLensProvider),
        vscode.languages.registerInlineValuesProvider({ language: 'python' }, new PyttdInlineValuesProvider()),
        vscode.window.registerTreeDataProvider('pyttd.callHistory', callHistoryProvider),
        vscode.commands.registerCommand('pyttd.gotoFirstExecution', (seq: number) => {
            vscode.debug.activeDebugSession?.customRequest('goto_frame', { target_seq: seq });
        }),
        vscode.commands.registerCommand('pyttd.gotoCallFrame', (seq: number) => {
            vscode.debug.activeDebugSession?.customRequest('goto_frame', { target_seq: seq });
        }),
    );

    // Debug session lifecycle
    context.subscriptions.push(
        vscode.debug.onDidStartDebugSession((session) => {
            if (session.type === 'pyttd') {
                codeLensProvider.refresh();
            }
        }),
        vscode.debug.onDidTerminateDebugSession((session) => {
            if (session.type === 'pyttd') {
                codeLensProvider.refresh();
                callHistoryProvider.refresh();
                callHistoryRefreshed = false;
            }
        }),
    );

    // Relay custom debug events to timeline webview + refresh call history on first timeline data
    context.subscriptions.push(
        vscode.debug.onDidReceiveDebugSessionCustomEvent((e) => {
            if (e.session.type !== 'pyttd') return;
            if (e.event === 'pyttd/timelineData' || e.event === 'pyttd/positionChanged') {
                timelineProvider.postMessage({ type: e.event, data: e.body });
            }
            // Refresh call history once when entering replay mode
            if (e.event === 'pyttd/timelineData' && !callHistoryRefreshed) {
                callHistoryRefreshed = true;
                callHistoryProvider.refresh();
            }
        }),
    );
}

export function deactivate() {}
