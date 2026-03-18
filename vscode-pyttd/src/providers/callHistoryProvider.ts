import * as vscode from 'vscode';
import * as path from 'path';

interface CallNode {
    callSeq: number;
    returnSeq: number | null;
    functionName: string;
    filename: string;
    line: number;
    depth: number;
    hasException: boolean;
    isComplete: boolean;
}

export class PyttdCallHistoryProvider implements vscode.TreeDataProvider<CallNode> {
    private _onDidChangeTreeData = new vscode.EventEmitter<CallNode | undefined | void>();
    public readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    public refresh(): void {
        this._onDidChangeTreeData.fire();
    }

    getTreeItem(element: CallNode): vscode.TreeItem {
        let label = element.functionName;
        if (!element.isComplete) {
            label += ' (incomplete)';
        }

        const item = new vscode.TreeItem(
            label,
            element.isComplete
                ? vscode.TreeItemCollapsibleState.Collapsed
                : vscode.TreeItemCollapsibleState.None
        );

        item.description = `${path.basename(element.filename)}:${element.line}`;
        item.tooltip = `${element.functionName} at ${element.filename}:${element.line} (seq ${element.callSeq}${element.returnSeq !== null ? '–' + element.returnSeq : ''})`;
        item.iconPath = element.hasException
            ? new vscode.ThemeIcon('error')
            : new vscode.ThemeIcon('symbol-function');
        item.command = {
            command: 'pyttd.gotoCallFrame',
            title: 'Go to Call',
            arguments: [element.callSeq],
        };

        return item;
    }

    async getChildren(element?: CallNode): Promise<CallNode[]> {
        const session = vscode.debug.activeDebugSession;
        if (!session || session.type !== 'pyttd') {
            return [];
        }

        const params: any = {};
        if (element) {
            params.parentCallSeq = element.callSeq;
            params.parentReturnSeq = element.returnSeq;
        }

        try {
            const resp = await session.customRequest('get_call_children', params);
            return resp?.children || [];
        } catch {
            return [];
        }
    }
}
