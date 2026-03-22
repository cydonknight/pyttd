import * as vscode from 'vscode';
import * as path from 'path';
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

    // Status bar item for checkpoint memory during recording
    const memoryStatusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    memoryStatusBar.command = 'pyttd.showCheckpointMemory';
    context.subscriptions.push(memoryStatusBar);

    let lastCheckpointMemoryInfo: any = null;

    context.subscriptions.push(
        vscode.commands.registerCommand('pyttd.showCheckpointMemory', () => {
            if (!lastCheckpointMemoryInfo) {
                vscode.window.showInformationMessage('No checkpoint memory data available.');
                return;
            }
            const info = lastCheckpointMemoryInfo;
            const items = [
                `Total: ${info.checkpointMemoryMB ?? 0} MB across ${info.checkpointCount ?? 0} checkpoints`,
            ];
            vscode.window.showQuickPick(items, { title: 'Checkpoint Memory' });
        }),
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
            vscode.debug.activeDebugSession?.customRequest('goto_frame', { targetSeq: seq });
        }),
        vscode.commands.registerCommand('pyttd.gotoCallFrame', (seq: number) => {
            vscode.debug.activeDebugSession?.customRequest('goto_frame', { targetSeq: seq });
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
                memoryStatusBar.text = '$(pulse) TTD: Recording...';
                memoryStatusBar.show();
                statusBar.startRecording();
            }
        }),
        vscode.debug.onDidTerminateDebugSession((session) => {
            if (session.type === 'pyttd') {
                codeLensProvider.refresh();
                callHistoryProvider.refresh();
                callHistoryRefreshed = false;
                memoryStatusBar.hide();
                lastCheckpointMemoryInfo = null;
                statusBar.reset();
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
            // Hide memory status bar when entering replay mode
            if (e.event === 'pyttd/timelineData') {
                memoryStatusBar.hide();
            }
            // Update status bar with checkpoint memory during recording
            if (e.event === 'pyttd/checkpointMemory') {
                lastCheckpointMemoryInfo = e.body;
                const count = e.body.checkpointCount ?? 0;
                const mb = e.body.checkpointMemoryMB ?? 0;
                memoryStatusBar.text = `$(database) TTD: ${count} checkpoints (${mb} MB)`;
                memoryStatusBar.tooltip = `Checkpoint memory: ${mb} MB across ${count} checkpoints. Click for details.`;
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
