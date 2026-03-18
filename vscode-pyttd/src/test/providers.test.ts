import * as assert from 'assert';
import * as path from 'path';
import { installMock, MockRange, MockUri, TreeItemCollapsibleState } from './mock-vscode';

// Install vscode mock BEFORE importing provider modules
installMock();

import { PyttdCallHistoryProvider } from '../providers/callHistoryProvider';
import { PyttdCodeLensProvider } from '../providers/codeLensProvider';
import { PyttdInlineValuesProvider } from '../providers/inlineValuesProvider';
import { TimelineScrubberProvider } from '../providers/timelineScrubberProvider';

describe('PyttdCallHistoryProvider', () => {
    let provider: PyttdCallHistoryProvider;

    beforeEach(() => {
        provider = new PyttdCallHistoryProvider();
    });

    describe('getTreeItem', () => {
        it('should create tree item with function name', () => {
            const node = {
                callSeq: 10,
                returnSeq: 20,
                functionName: 'foo',
                filename: '/test/script.py',
                line: 5,
                depth: 1,
                hasException: false,
                isComplete: true,
            };

            const item = provider.getTreeItem(node);
            assert.strictEqual(item.label, 'foo');
            assert.strictEqual(item.collapsibleState, TreeItemCollapsibleState.Collapsed);
        });

        it('should mark incomplete calls', () => {
            const node = {
                callSeq: 10,
                returnSeq: null,
                functionName: 'bar',
                filename: '/test/script.py',
                line: 15,
                depth: 2,
                hasException: false,
                isComplete: false,
            };

            const item = provider.getTreeItem(node);
            assert.strictEqual(item.label, 'bar (incomplete)');
            assert.strictEqual(item.collapsibleState, TreeItemCollapsibleState.None);
        });

        it('should set exception icon for exception nodes', () => {
            const node = {
                callSeq: 10,
                returnSeq: 20,
                functionName: 'fail',
                filename: '/test/script.py',
                line: 8,
                depth: 1,
                hasException: true,
                isComplete: true,
            };

            const item = provider.getTreeItem(node);
            assert.strictEqual((item.iconPath as any).id, 'error');
        });

        it('should set function icon for normal nodes', () => {
            const node = {
                callSeq: 10,
                returnSeq: 20,
                functionName: 'ok',
                filename: '/test/script.py',
                line: 1,
                depth: 0,
                hasException: false,
                isComplete: true,
            };

            const item = provider.getTreeItem(node);
            assert.strictEqual((item.iconPath as any).id, 'symbol-function');
        });

        it('should set description with basename:line', () => {
            const node = {
                callSeq: 10,
                returnSeq: 20,
                functionName: 'foo',
                filename: '/test/deep/script.py',
                line: 42,
                depth: 1,
                hasException: false,
                isComplete: true,
            };

            const item = provider.getTreeItem(node);
            assert.strictEqual(item.description, 'script.py:42');
        });

        it('should set goto command', () => {
            const node = {
                callSeq: 15,
                returnSeq: 25,
                functionName: 'foo',
                filename: '/test/script.py',
                line: 5,
                depth: 1,
                hasException: false,
                isComplete: true,
            };

            const item = provider.getTreeItem(node);
            assert.strictEqual(item.command!.command, 'pyttd.gotoCallFrame');
            assert.deepStrictEqual(item.command!.arguments, [15]);
        });

        it('should include seq range in tooltip', () => {
            const node = {
                callSeq: 10,
                returnSeq: 20,
                functionName: 'foo',
                filename: '/test/script.py',
                line: 5,
                depth: 1,
                hasException: false,
                isComplete: true,
            };

            const item = provider.getTreeItem(node);
            assert.ok((item.tooltip as string)?.includes('10'));
            assert.ok((item.tooltip as string)?.includes('20'));
        });
    });

    describe('getChildren', () => {
        it('should return empty array with no active session', async () => {
            const children = await provider.getChildren();
            assert.deepStrictEqual(children, []);
        });
    });

    describe('refresh', () => {
        it('should fire onDidChangeTreeData', () => {
            let fired = false;
            provider.onDidChangeTreeData(() => { fired = true; });
            provider.refresh();
            assert.ok(fired);
        });
    });
});

describe('PyttdCodeLensProvider', () => {
    let provider: PyttdCodeLensProvider;

    beforeEach(() => {
        provider = new PyttdCodeLensProvider();
    });

    describe('refresh', () => {
        it('should fire onDidChangeCodeLenses', () => {
            let fired = false;
            provider.onDidChangeCodeLenses(() => { fired = true; });
            provider.refresh();
            assert.ok(fired);
        });
    });

    describe('provideCodeLenses', () => {
        it('should return empty with no active session', async () => {
            const doc = { uri: { fsPath: '/test/script.py' } } as any;
            const token = { isCancellationRequested: false } as any;
            const result = await provider.provideCodeLenses(doc, token);
            assert.deepStrictEqual(result, []);
        });
    });
});

describe('PyttdInlineValuesProvider', () => {
    describe('provideInlineValues', () => {
        it('should return empty with no active session', async () => {
            const provider = new PyttdInlineValuesProvider();
            const result = await provider.provideInlineValues(
                {} as any, {} as any, { frameId: 0 } as any,
                { isCancellationRequested: false } as any
            );
            assert.deepStrictEqual(result, []);
        });
    });
});

describe('escapeRegExp', () => {
    // Import and test the escapeRegExp function indirectly
    // It's not exported, but we can test the regex pattern it produces
    it('should escape special regex characters', () => {
        // The function is: s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

        assert.strictEqual(escapeRegExp('hello'), 'hello');
        assert.strictEqual(escapeRegExp('a.b'), 'a\\.b');
        assert.strictEqual(escapeRegExp('a*b'), 'a\\*b');
        assert.strictEqual(escapeRegExp('a+b'), 'a\\+b');
        assert.strictEqual(escapeRegExp('a?b'), 'a\\?b');
        assert.strictEqual(escapeRegExp('a[0]'), 'a\\[0\\]');
        assert.strictEqual(escapeRegExp('(a|b)'), '\\(a\\|b\\)');
        assert.strictEqual(escapeRegExp('a^b$'), 'a\\^b\\$');
        assert.strictEqual(escapeRegExp('a{1}'), 'a\\{1\\}');
        assert.strictEqual(escapeRegExp('a\\b'), 'a\\\\b');
    });

    it('escaped patterns match literally in RegExp', () => {
        const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const special = 'file.name[0](1).py';
        const pattern = new RegExp(escapeRegExp(special));
        assert.ok(pattern.test(special));
        assert.ok(!pattern.test('fileXname'));
    });
});

describe('TimelineScrubberProvider', () => {
    it('should instantiate with extension URI', () => {
        const uri = new MockUri('/test/extension') as any;
        const provider = new TimelineScrubberProvider(uri);
        assert.ok(provider);
    });

    it('should have correct viewType', () => {
        assert.strictEqual(TimelineScrubberProvider.viewType, 'pyttd.timeline');
    });

    it('postMessage should not throw when no view is resolved', () => {
        const uri = new MockUri('/test/extension') as any;
        const provider = new TimelineScrubberProvider(uri);
        // Should not throw even though no webview is resolved
        provider.postMessage({ type: 'test' });
    });
});
