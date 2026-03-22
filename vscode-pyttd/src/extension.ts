import * as vscode from 'vscode';
import * as path from 'path';
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

    // P1-3: Reverse navigation keybindings
    context.subscriptions.push(
        vscode.commands.registerCommand('pyttd.stepBack', () => {
            vscode.commands.executeCommand('workbench.action.debug.stepBack');
        }),
        vscode.commands.registerCommand('pyttd.reverseContinue', () => {
            vscode.commands.executeCommand('workbench.action.debug.reverseContinue');
        }),
    );

    // P1-3: Export Perfetto Trace command
    context.subscriptions.push(
        vscode.commands.registerCommand('pyttd.exportPerfetto', async () => {
            const session = vscode.debug.activeDebugSession;
            if (!session || session.type !== 'pyttd') {
                vscode.window.showWarningMessage('No active pyttd debug session.');
                return;
            }
            const uri = await vscode.window.showSaveDialog({
                defaultUri: vscode.Uri.file('trace.json'),
                filters: { 'JSON Files': ['json'] },
            });
            if (!uri) return;
            vscode.window.showInformationMessage(
                `Export: Run 'pyttd export --format perfetto --db <your.pyttd.db> -o ${uri.fsPath}' in terminal.`
            );
        }),
    );

    // P1-3: Show Variable History command
    context.subscriptions.push(
        vscode.commands.registerCommand('pyttd.showVariableHistory', async () => {
            const session = vscode.debug.activeDebugSession;
            if (!session || session.type !== 'pyttd') {
                vscode.window.showWarningMessage('No active pyttd debug session.');
                return;
            }
            const varName = await vscode.window.showInputBox({
                prompt: 'Variable name to show history for',
                placeHolder: 'e.g., x',
            });
            if (!varName) return;
            try {
                const result = await session.customRequest('get_variable_history', {
                    variableName: varName,
                    startSeq: 0,
                    endSeq: 999999999,
                    maxPoints: 100,
                });
                const channel = vscode.window.createOutputChannel('pyttd Variable History');
                channel.clear();
                channel.appendLine(`History of '${varName}':`);
                for (const entry of result.history || []) {
                    channel.appendLine(`  seq ${entry.seq}: ${entry.value} (${entry.filename}:${entry.line})`);
                }
                if (!result.history || result.history.length === 0) {
                    channel.appendLine('  (no changes found in recording)');
                }
                channel.show();
            } catch (e: any) {
                vscode.window.showErrorMessage(`Failed to get variable history: ${e.message}`);
            }
        }),
    );

    // P1-3: Debug This File (editor context menu)
    context.subscriptions.push(
        vscode.commands.registerCommand('pyttd.debugThisFile', () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) return;
            const filePath = editor.document.uri.fsPath;
            if (!filePath || filePath.startsWith('Untitled')) {
                vscode.window.showWarningMessage('Save the file before debugging.');
                return;
            }
            vscode.debug.startDebugging(vscode.workspace.workspaceFolders?.[0], {
                type: 'pyttd',
                request: 'launch',
                name: `Time-Travel Debug: ${path.basename(filePath)}`,
                program: filePath,
            });
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

    // Relay custom debug events to timeline webview, handle errors, refresh call history
    context.subscriptions.push(
        vscode.debug.onDidReceiveDebugSessionCustomEvent((e) => {
            if (e.session.type !== 'pyttd') return;
            if (e.event === 'pyttd/timelineData' || e.event === 'pyttd/positionChanged') {
                timelineProvider.postMessage({ type: e.event, data: e.body });
            }
            // P1-2: Surface errors as VSCode notifications
            if (e.event === 'pyttd/error') {
                const { message, detail, severity } = e.body;
                const fullMsg = detail ? `${message}\n${detail}` : message;
                if (severity === 'error') {
                    vscode.window.showErrorMessage(fullMsg);
                } else if (severity === 'warning') {
                    vscode.window.showWarningMessage(fullMsg);
                }
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
