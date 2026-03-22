import * as assert from 'assert';

/**
 * Tests for PyttdStatusBarProvider state transitions.
 * Uses a minimal mock of vscode.window.createStatusBarItem.
 */

class MockStatusBarItem {
    text = '';
    tooltip: string | undefined;
    command: string | undefined;
    backgroundColor: any;
    private _visible = false;

    show() { this._visible = true; }
    hide() { this._visible = false; }
    dispose() { this._visible = false; }
    get isVisible() { return this._visible; }
}

class MockThemeColor {
    constructor(public id: string) {}
}

// Minimal mock of vscode module for status bar tests
function setupVscodeMock() {
    const items: MockStatusBarItem[] = [];
    const mockVscode = {
        StatusBarAlignment: { Left: 1, Right: 2 },
        ThemeColor: MockThemeColor,
        window: {
            createStatusBarItem: (_alignment: number, _priority: number) => {
                const item = new MockStatusBarItem();
                items.push(item);
                return item;
            },
        },
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

    return { mockVscode, items };
}

describe('PyttdStatusBarProvider', () => {
    let PyttdStatusBarProvider: any;
    let items: MockStatusBarItem[];

    before(() => {
        const mock = setupVscodeMock();
        items = mock.items;
        // Import after mock is installed
        PyttdStatusBarProvider = require('../providers/statusBarProvider').PyttdStatusBarProvider;
    });

    after(() => {
        delete require.cache['vscode'];
    });

    it('should create two status bar items', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        assert.strictEqual(items.length, 2);
        provider.dispose();
    });

    it('should start hidden', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];
        const warningItem = items[1];
        assert.strictEqual(mainItem.isVisible, false);
        assert.strictEqual(warningItem.isVisible, false);
        provider.dispose();
    });

    it('should show recording state', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];
        const warningItem = items[1];

        provider.startRecording();
        assert.strictEqual(mainItem.isVisible, true);
        assert.ok(mainItem.text.includes('Recording'));
        assert.ok(mainItem.text.includes('0'));
        assert.strictEqual(warningItem.isVisible, false);
        provider.dispose();
    });

    it('should update recording stats', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];

        provider.startRecording();
        provider.updateRecording({ frameCount: 5000 });
        assert.ok(mainItem.text.includes('5'));
        assert.ok(mainItem.text.includes('Recording'));
        provider.dispose();
    });

    it('should show warning for dropped frames', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const warningItem = items[1];

        provider.startRecording();
        provider.updateRecording({ frameCount: 1000, droppedFrames: 5 });
        assert.strictEqual(warningItem.isVisible, true);
        assert.ok(warningItem.text.includes('5 dropped'));
        provider.dispose();
    });

    it('should show warning for pool overflows', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const warningItem = items[1];

        provider.startRecording();
        provider.updateRecording({ frameCount: 1000, poolOverflows: 3 });
        assert.strictEqual(warningItem.isVisible, true);
        assert.ok(warningItem.text.includes('3 overflows'));
        provider.dispose();
    });

    it('should enter replay state', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];
        const warningItem = items[1];

        provider.enterReplay(50, 1000);
        assert.strictEqual(mainItem.isVisible, true);
        assert.ok(mainItem.text.includes('Replay'));
        assert.ok(mainItem.text.includes('50'));
        assert.ok(mainItem.text.includes('1'));
        assert.strictEqual(warningItem.isVisible, false);
        provider.dispose();
    });

    it('should update position in replay', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];

        provider.enterReplay(0, 1000);
        provider.updatePosition(500);
        assert.ok(mainItem.text.includes('500'));
        assert.ok(mainItem.text.includes('Replay'));
        provider.dispose();
    });

    it('should not update position if not in replay', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];

        provider.startRecording();
        const textBefore = mainItem.text;
        provider.updatePosition(500);
        // Text should not have changed to replay mode
        assert.ok(mainItem.text.includes('Recording'));
        provider.dispose();
    });

    it('should reset to idle', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];
        const warningItem = items[1];

        provider.startRecording();
        provider.reset();
        assert.strictEqual(mainItem.isVisible, false);
        assert.strictEqual(warningItem.isVisible, false);
        provider.dispose();
    });

    it('should have focus command on main item', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];
        assert.strictEqual(mainItem.command, 'pyttd.focusTimeline');
        provider.dispose();
    });

    it('should transition from recording to replay', () => {
        items.length = 0;
        const provider = new PyttdStatusBarProvider();
        const mainItem = items[0];

        provider.startRecording();
        assert.ok(mainItem.text.includes('Recording'));

        provider.enterReplay(0, 500);
        assert.ok(mainItem.text.includes('Replay'));
        assert.ok(!mainItem.text.includes('Recording'));
        provider.dispose();
    });
});
