import * as vscode from 'vscode';

export class PyttdInlineValuesProvider implements vscode.InlineValuesProvider {
    async provideInlineValues(
        document: vscode.TextDocument,
        viewPort: vscode.Range,
        context: vscode.InlineValueContext,
        token: vscode.CancellationToken
    ): Promise<vscode.InlineValue[]> {
        const session = vscode.debug.activeDebugSession;
        if (!session || session.type !== 'pyttd') {
            return [];
        }

        const frameId = context.frameId;

        let variables: { name: string; value: string; type?: string }[];
        try {
            const resp = await session.customRequest('get_variables', {
                seq: frameId,
            });
            variables = resp?.variables || [];
        } catch {
            return [];
        }

        if (token.isCancellationRequested) return [];

        const results: vscode.InlineValue[] = [];
        const startLine = viewPort.start.line;
        const endLine = viewPort.end.line;

        for (const v of variables) {
            // Skip dunder variables
            if (v.name.startsWith('__') && v.name.endsWith('__')) continue;

            const pattern = new RegExp(`\\b${escapeRegExp(v.name)}\\b`);

            for (let lineNum = startLine; lineNum <= endLine; lineNum++) {
                const lineText = document.lineAt(lineNum).text;
                const match = pattern.exec(lineText);
                if (match) {
                    const range = new vscode.Range(
                        lineNum,
                        match.index,
                        lineNum,
                        match.index + v.name.length
                    );
                    results.push(
                        new vscode.InlineValueText(range, `${v.name} = ${v.value}`)
                    );
                }
            }
        }

        return results;
    }
}

function escapeRegExp(s: string): string {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
