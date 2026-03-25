import * as vscode from 'vscode';
import * as path from 'path';

function shortName(qualname: string): string {
    const lastDot = qualname.lastIndexOf('.');
    return lastDot >= 0 ? qualname.substring(lastDot + 1) : qualname;
}

interface CallNode {
    callSeq: number;
    returnSeq: number | null;
    functionName: string;
    filename: string;
    line: number;
    depth: number;
    hasException: boolean;
    isComplete: boolean;
    isCoroutine?: boolean;
    suspendCount?: number;
}

export class PyttdCallHistoryProvider implements vscode.TreeDataProvider<CallNode> {
    private _onDidChangeTreeData = new vscode.EventEmitter<CallNode | undefined | void>();
    public readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    public refresh(): void {
        this._onDidChangeTreeData.fire();
    }

    getTreeItem(element: CallNode): vscode.TreeItem {
        const sname = shortName(element.functionName);
        let label = sname;
        if (element.suspendCount && element.suspendCount > 0) {
            label += ` (${element.suspendCount} await${element.suspendCount > 1 ? 's' : ''})`;
        }
        if (!element.isComplete) {
            label += ' (incomplete)';
        }

        const item = new vscode.TreeItem(
            label,
            element.isComplete
                ? vscode.TreeItemCollapsibleState.Collapsed
                : vscode.TreeItemCollapsibleState.None
        );

        const fileInfo = `${path.basename(element.filename)}:${element.line}`;
        if (sname !== element.functionName) {
            item.description = element.functionName;
        } else {
            item.description = fileInfo;
        }
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
