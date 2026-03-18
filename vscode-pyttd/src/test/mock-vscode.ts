/**
 * Minimal mock of the `vscode` module for unit testing.
 * Register in require.cache before importing modules that depend on 'vscode'.
 */

export class MockEventEmitter<T> {
    private listeners: Array<(e: T) => void> = [];
    event = (listener: (e: T) => void) => {
        this.listeners.push(listener);
        return { dispose: () => { this.listeners = this.listeners.filter(l => l !== listener); } };
    };
    fire(data: T) { for (const l of this.listeners) l(data); }
    dispose() { this.listeners = []; }
}

export class MockRange {
    constructor(
        public startLine: number, public startChar: number,
        public endLine: number, public endChar: number
    ) {}
    get start() { return { line: this.startLine, character: this.startChar }; }
    get end() { return { line: this.endLine, character: this.endChar }; }
}

export class MockCodeLens {
    constructor(public range: MockRange, public command?: any) {}
}

export class MockTreeItem {
    label: string;
    collapsibleState: number;
    description?: string;
    tooltip?: string;
    iconPath?: any;
    command?: any;
    constructor(label: string, collapsibleState: number = 0) {
        this.label = label;
        this.collapsibleState = collapsibleState;
    }
}

export class MockThemeIcon {
    constructor(public id: string) {}
}

export class MockInlineValueText {
    constructor(public range: MockRange, public text: string) {}
}

export class MockUri {
    constructor(public fsPath: string) {}
    static joinPath(base: MockUri, ...parts: string[]): MockUri {
        return new MockUri([base.fsPath, ...parts].join('/'));
    }
}

export const TreeItemCollapsibleState = {
    None: 0,
    Collapsed: 1,
    Expanded: 2,
};

/** Install mock into require.cache so `require('vscode')` returns this. */
export function installMock(activeSession: any = null): void {
    const mockVscode = {
        EventEmitter: MockEventEmitter,
        Range: MockRange,
        CodeLens: MockCodeLens,
        TreeItem: MockTreeItem,
        TreeItemCollapsibleState,
        ThemeIcon: MockThemeIcon,
        InlineValueText: MockInlineValueText,
        Uri: MockUri,
        debug: {
            activeDebugSession: activeSession,
        },
        window: {},
        languages: {},
        commands: {},
        CancellationTokenSource: class { token = { isCancellationRequested: false }; },
    };

    // Inject into require cache
    const Module = require('module');
    const resolveFilename = Module._resolveFilename;
    Module._resolveFilename = function (request: string, ...args: any[]) {
        if (request === 'vscode') return 'vscode';
        return resolveFilename.call(this, request, ...args);
    };
    require.cache['vscode'] = {
        id: 'vscode',
        filename: 'vscode',
        loaded: true,
        exports: mockVscode,
    } as any;

    return mockVscode as any;
}
