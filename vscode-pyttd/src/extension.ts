import * as vscode from 'vscode';
import { PyttdDebugSession } from './debugAdapter/pyttdDebugSession';

export function activate(context: vscode.ExtensionContext) {
    context.subscriptions.push(
        vscode.debug.registerDebugAdapterDescriptorFactory('pyttd', {
            createDebugAdapterDescriptor(_session: vscode.DebugSession): vscode.ProviderResult<vscode.DebugAdapterDescriptor> {
                return new vscode.DebugAdapterInlineImplementation(new PyttdDebugSession());
            }
        })
    );
    // Phase 5: register TimelineScrubberProvider
    // Phase 6: register CodeLensProvider, InlineValuesProvider, CallHistoryProvider
}

export function deactivate() {}
