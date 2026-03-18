import * as vscode from 'vscode';

interface ExecutionStat {
    functionName: string;
    callCount: number;
    exceptionCount: number;
    firstCallSeq: number;
    defLine: number;
}

export class PyttdCodeLensProvider implements vscode.CodeLensProvider {
    private _onDidChangeCodeLenses = new vscode.EventEmitter<void>();
    public readonly onDidChangeCodeLenses = this._onDidChangeCodeLenses.event;

    /** Map from fsPath to DB filename */
    private tracedFiles = new Map<string, string>();

    public refresh(): void {
        this.tracedFiles.clear();
        this._onDidChangeCodeLenses.fire();
    }

    async provideCodeLenses(
        document: vscode.TextDocument,
        token: vscode.CancellationToken
    ): Promise<vscode.CodeLens[]> {
        const session = vscode.debug.activeDebugSession;
        if (!session || session.type !== 'pyttd') {
            return [];
        }

        // Populate traced files cache on first call
        if (this.tracedFiles.size === 0) {
            try {
                const resp = await session.customRequest('get_traced_files', {});
                const files: string[] = resp?.files || [];
                for (const dbFile of files) {
                    // Use the DB filename as-is for the key
                    // (it's typically an absolute path from PyCode_GetFilename)
                    this.tracedFiles.set(dbFile, dbFile);
                }
            } catch {
                return [];
            }
        }

        if (token.isCancellationRequested) return [];

        // Find the DB filename that matches this document
        const fsPath = document.uri.fsPath;
        let dbFilename: string | undefined;
        for (const [dbFile] of this.tracedFiles) {
            if (dbFile === fsPath || fsPath.endsWith(dbFile) || dbFile.endsWith(fsPath)) {
                dbFilename = dbFile;
                break;
            }
        }

        if (!dbFilename) return [];

        try {
            const resp = await session.customRequest('get_execution_stats', {
                filename: dbFilename,
            });
            const stats: ExecutionStat[] = resp?.stats || [];
            if (token.isCancellationRequested) return [];

            return stats.map((stat) => {
                const line = (stat.defLine || 1) - 1; // 0-indexed
                const range = new vscode.Range(line, 0, line, 0);
                let label = `TTD: ${stat.callCount} call${stat.callCount !== 1 ? 's' : ''}`;
                if (stat.exceptionCount > 0) {
                    label += ` | ${stat.exceptionCount} exception${stat.exceptionCount !== 1 ? 's' : ''}`;
                }
                return new vscode.CodeLens(range, {
                    title: label,
                    command: 'pyttd.gotoFirstExecution',
                    arguments: [stat.firstCallSeq],
                });
            });
        } catch {
            return [];
        }
    }
}
