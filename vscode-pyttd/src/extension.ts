import * as vscode from 'vscode';
import { PyttdDebugSession } from './debugAdapter/pyttdDebugSession';
import { TimelineScrubberProvider } from './providers/timelineScrubberProvider';
import { PyttdCodeLensProvider } from './providers/codeLensProvider';
import { PyttdInlineValuesProvider } from './providers/inlineValuesProvider';
import { PyttdCallHistoryProvider } from './providers/callHistoryProvider';
import { PyttdStatusBarProvider } from './providers/statusBarProvider';

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

    // Status bar provider
    const statusBar = new PyttdStatusBarProvider();
    context.subscriptions.push(statusBar);

    // Register command to focus timeline view
    context.subscriptions.push(
        vscode.commands.registerCommand('pyttd.focusTimeline', () => {
            // Focus the timeline webview in the debug sidebar
            vscode.commands.executeCommand('pyttd.timeline.focus');
        }),
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
                statusBar.startRecording();
            }
        }),
        vscode.debug.onDidTerminateDebugSession((session) => {
            if (session.type === 'pyttd') {
                codeLensProvider.refresh();
                callHistoryProvider.refresh();
                callHistoryRefreshed = false;
                statusBar.reset();
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
                statusBar.enterReplay(e.body.startSeq || 0, e.body.totalFrames || 0);
            }
            // Update status bar position on navigation
            if (e.event === 'pyttd/positionChanged') {
                statusBar.updatePosition(e.body.seq);
            }
            // Update status bar during recording
            if (e.event === 'pyttd/recordingProgress') {
                statusBar.updateRecording(e.body);
            }
        }),
    );
}

export function deactivate() {}
