import * as assert from 'assert';
import * as net from 'net';
import { DebugProtocol } from '@vscode/debugprotocol';
import { PyttdDebugSession } from '../debugAdapter/pyttdDebugSession';

/**
 * Tests for PyttdDebugSession using a mock JSON-RPC server.
 * The session connects to a real TCP server that responds to RPC calls,
 * letting us test the full DAP handler → RPC → response → event chain.
 */

/** Captures events and responses from a debug session. */
class SessionHarness {
    session: PyttdDebugSession;
    events: Array<{ type: string; body: any }> = [];
    responses: Array<{ command: string; success: boolean; body?: any; message?: string }> = [];
    private eventWaiters: Array<{ type: string; resolve: (e: any) => void }> = [];

    constructor() {
        this.session = new PyttdDebugSession();

        // Intercept sendEvent
        const origSendEvent = (this.session as any).sendEvent.bind(this.session);
        (this.session as any).sendEvent = (event: any) => {
            const entry = { type: event.event || event.constructor?.name, body: event.body || {} };
            this.events.push(entry);
            origSendEvent(event);

            // Resolve any waiters
            for (let i = this.eventWaiters.length - 1; i >= 0; i--) {
                if (this.eventWaiters[i].type === entry.type) {
                    this.eventWaiters[i].resolve(entry);
                    this.eventWaiters.splice(i, 1);
                }
            }
        };

        // Intercept sendResponse
        const origSendResponse = (this.session as any).sendResponse.bind(this.session);
        (this.session as any).sendResponse = (response: DebugProtocol.Response) => {
            this.responses.push({
                command: response.command,
                success: response.success,
                body: response.body,
                message: response.message,
            });
            origSendResponse(response);
        };

        // Intercept sendErrorResponse
        const origSendErrorResponse = (this.session as any).sendErrorResponse.bind(this.session);
        (this.session as any).sendErrorResponse = (response: DebugProtocol.Response, code: number, msg: string) => {
            this.responses.push({
                command: response.command,
                success: false,
                message: msg,
            });
            origSendErrorResponse(response, code, msg);
        };
    }

    /** Wait for an event of the given type (e.g., 'stopped', 'terminated'). */
    waitForEvent(type: string, timeout = 2000): Promise<{ type: string; body: any }> {
        // Check already captured
        const existing = this.events.find(e => e.type === type);
        if (existing) return Promise.resolve(existing);

        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error(`Timeout waiting for event: ${type}`)), timeout);
            this.eventWaiters.push({
                type,
                resolve: (e) => { clearTimeout(timer); resolve(e); },
            });
        });
    }

    /** Get the last response for a command. */
    lastResponse(command: string) {
        return [...this.responses].reverse().find(r => r.command === command);
    }

    clearEvents() { this.events = []; }
    clearResponses() { this.responses = []; }
}

/** Mock JSON-RPC server that auto-responds to requests. */
class MockRpcServer {
    server: net.Server;
    socket: net.Socket | null = null;
    port = 0;
    handlers: Map<string, (params: any) => any> = new Map();
    receivedRequests: Array<{ method: string; params: any }> = [];

    constructor() {
        this.server = net.createServer((sock) => {
            this.socket = sock;
            let buffer = Buffer.alloc(0);
            sock.on('data', (data: Buffer) => {
                buffer = Buffer.concat([buffer, data]);
                while (true) {
                    const headerEnd = buffer.indexOf('\r\n\r\n');
                    if (headerEnd < 0) break;
                    const header = buffer.subarray(0, headerEnd).toString('ascii');
                    let contentLength = 0;
                    for (const line of header.split('\r\n')) {
                        if (line.toLowerCase().startsWith('content-length:')) {
                            contentLength = parseInt(line.split(':')[1].trim(), 10);
                        }
                    }
                    const bodyStart = headerEnd + 4;
                    if (buffer.length < bodyStart + contentLength) break;
                    const body = buffer.subarray(bodyStart, bodyStart + contentLength).toString('utf-8');
                    buffer = buffer.subarray(bodyStart + contentLength);
                    try {
                        const msg = JSON.parse(body);
                        if (msg.id != null) {
                            this.receivedRequests.push({ method: msg.method, params: msg.params });
                            const handler = this.handlers.get(msg.method);
                            const result = handler ? handler(msg.params) : {};
                            this.sendMessage({ jsonrpc: '2.0', id: msg.id, result });
                        }
                    } catch {}
                }
            });
        });
    }

    sendMessage(msg: any) {
        if (!this.socket) return;
        const body = JSON.stringify(msg);
        this.socket.write(`Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
    }

    sendNotification(method: string, params: any) {
        this.sendMessage({ jsonrpc: '2.0', method, params });
    }

    async start(): Promise<void> {
        return new Promise((resolve) => {
            this.server.listen(0, '127.0.0.1', () => {
                this.port = (this.server.address() as net.AddressInfo).port;
                resolve();
            });
        });
    }

    async stop(): Promise<void> {
        if (this.socket) this.socket.destroy();
        return new Promise((resolve) => this.server.close(() => resolve()));
    }
}

describe('PyttdDebugSession', () => {
    describe('initializeRequest', () => {
        it('should advertise capabilities', () => {
            const harness = new SessionHarness();
            const response = { command: 'initialize', success: true, body: {} } as DebugProtocol.InitializeResponse;
            (harness.session as any).initializeRequest(
                response,
                {} as DebugProtocol.InitializeRequestArguments
            );

            const body = response.body!;
            assert.strictEqual(body.supportsConfigurationDoneRequest, true);
            assert.strictEqual(body.supportsEvaluateForHovers, true);
            assert.strictEqual(body.supportsStepBack, true);
            assert.strictEqual(body.supportsGotoTargetsRequest, true);
            assert.strictEqual(body.supportsRestartFrame, true);
            assert.strictEqual(body.supportsConditionalBreakpoints, true);
        });
    });

    describe('launchRequest', () => {
        it('should error if no program or module', () => {
            const harness = new SessionHarness();
            const response = { command: 'launch', success: true } as DebugProtocol.LaunchResponse;
            (harness.session as any).launchRequest(
                response,
                { cwd: '/tmp', pythonPath: '/usr/bin/python3' }
            );

            const last = harness.lastResponse('launch');
            assert.strictEqual(last?.success, false);
            assert.ok(last?.message?.includes('program'));
        });
    });

    describe('scopesRequest', () => {
        it('should encode variablesReference as frameId + 1', () => {
            const harness = new SessionHarness();
            const response = { command: 'scopes', success: true } as DebugProtocol.ScopesResponse;
            (harness.session as any).scopesRequest(response, { frameId: 42 });

            assert.strictEqual(response.body.scopes.length, 1);
            assert.strictEqual(response.body.scopes[0].name, 'Locals');
            assert.strictEqual(response.body.scopes[0].variablesReference, 43);
        });
    });

    describe('navigation guards', () => {
        const navMethods = [
            'continueRequest', 'nextRequest', 'stepInRequest', 'stepOutRequest',
            'stepBackRequest', 'reverseContinueRequest',
        ];

        for (const method of navMethods) {
            it(`${method} should return early when not replaying`, () => {
                const harness = new SessionHarness();
                const response = { command: method, success: true } as any;
                (harness.session as any)[method](response, {});

                // Should have sent a response (not an error)
                assert.ok(harness.responses.length >= 1);
            });
        }
    });

    describe('with mock backend', () => {
        let mockServer: MockRpcServer;
        let harness: SessionHarness;

        beforeEach(async () => {
            mockServer = new MockRpcServer();
            mockServer.handlers.set('backend_init', () => ({ version: '0.3.0' }));
            mockServer.handlers.set('launch', () => ({}));
            mockServer.handlers.set('configuration_done', () => ({}));
            mockServer.handlers.set('get_timeline_summary', () => ({ buckets: [] }));
            mockServer.handlers.set('set_breakpoints', () => ({}));
            mockServer.handlers.set('set_exception_breakpoints', () => ({}));
            mockServer.handlers.set('get_threads', () => ({
                threads: [{ id: 1, name: 'Main Thread' }, { id: 12345, name: 'Thread 12345' }]
            }));
            mockServer.handlers.set('get_stack_trace', () => ({
                frames: [
                    { seq: 10, name: 'foo', file: '/test/script.py', line: 5 },
                    { seq: 5, name: '<module>', file: '/test/script.py', line: 1 },
                ]
            }));
            mockServer.handlers.set('get_variables', () => ({
                variables: [{ name: 'x', value: '42', type: 'int' }]
            }));
            mockServer.handlers.set('evaluate', (p: any) => ({
                result: `${p.expression} = 42`, type: 'int'
            }));
            mockServer.handlers.set('continue', () => ({ seq: 50, reason: 'breakpoint' }));
            mockServer.handlers.set('next', () => ({ seq: 11, reason: 'step' }));
            mockServer.handlers.set('step_in', () => ({ seq: 12, reason: 'step' }));
            mockServer.handlers.set('step_out', () => ({ seq: 20, reason: 'step' }));
            mockServer.handlers.set('step_back', () => ({ seq: 9, reason: 'step' }));
            mockServer.handlers.set('reverse_continue', () => ({ seq: 1, reason: 'start' }));
            mockServer.handlers.set('goto_frame', (p: any) => ({ seq: p.target_seq, reason: 'goto' }));
            mockServer.handlers.set('goto_targets', () => ({
                targets: [{ seq: 10, function_name: 'foo' }]
            }));
            mockServer.handlers.set('restart_frame', () => ({ seq: 5, reason: 'goto' }));
            mockServer.handlers.set('disconnect', () => ({}));

            await mockServer.start();

            harness = new SessionHarness();

            // Connect session's backend directly to mock server
            const backend = (harness.session as any).backend;
            await backend.connect(mockServer.port);
            // Wait for server to accept the connection
            await new Promise<void>((resolve, reject) => {
                const deadline = Date.now() + 1000;
                const check = () => {
                    if (mockServer.socket) return resolve();
                    if (Date.now() > deadline) return reject(new Error('server socket not ready'));
                    setTimeout(check, 5);
                };
                check();
            });

            // Simulate entering replay mode
            (harness.session as any).isReplaying = true;
            (harness.session as any).currentSeq = 10;
            (harness.session as any).totalFrames = 100;
        });

        afterEach(async () => {
            try { (harness.session as any).backend.close(); } catch {}
            await mockServer.stop();
        });

        it('should get threads', async () => {
            const response = { command: 'threads', success: true } as DebugProtocol.ThreadsResponse;
            (harness.session as any).threadsRequest(response);
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('threads');
            assert.ok(last?.success);
            assert.strictEqual(last?.body?.threads?.length, 2);
            assert.strictEqual(last?.body?.threads[0].name, 'Main Thread');
        });

        it('should get stack trace', async () => {
            const response = { command: 'stackTrace', success: true } as DebugProtocol.StackTraceResponse;
            (harness.session as any).stackTraceRequest(response, { threadId: 1 });
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('stackTrace');
            assert.ok(last?.success);
            assert.strictEqual(last?.body?.stackFrames?.length, 2);
            assert.strictEqual(last?.body?.stackFrames[0].name, 'foo');
            assert.strictEqual(last?.body?.stackFrames[0].line, 5);
        });

        it('should get variables with decoded reference', async () => {
            const response = { command: 'variables', success: true } as DebugProtocol.VariablesResponse;
            (harness.session as any).variablesRequest(response, { variablesReference: 11 }); // seq = 10
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('variables');
            assert.ok(last?.success);
            assert.strictEqual(last?.body?.variables?.length, 1);
            assert.strictEqual(last?.body?.variables[0].name, 'x');
            assert.strictEqual(last?.body?.variables[0].value, '42');

            // Verify the backend received seq=10 (variablesReference - 1)
            const req = mockServer.receivedRequests.find(r => r.method === 'get_variables');
            assert.strictEqual(req?.params.seq, 10);
        });

        it('should evaluate expression', async () => {
            const response = { command: 'evaluate', success: true } as DebugProtocol.EvaluateResponse;
            (harness.session as any).evaluateRequest(response, {
                expression: 'x + 1', context: 'hover', frameId: 10
            });
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('evaluate');
            assert.ok(last?.success);
            assert.ok(last?.body?.result?.includes('42'));
        });

        it('should continue and emit stopped event', async () => {
            harness.clearEvents();
            const response = { command: 'continue', success: true } as DebugProtocol.ContinueResponse;
            (harness.session as any).continueRequest(response, { threadId: 1 });
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('continue');
            assert.ok(last?.success);

            // Should have emitted a stopped event with reason 'breakpoint'
            const stopped = harness.events.find(e => e.type === 'stopped');
            assert.ok(stopped, 'should emit stopped event');
            assert.strictEqual(stopped!.body.reason, 'breakpoint');
            assert.strictEqual((harness.session as any).currentSeq, 50);
        });

        it('should step_in and update currentSeq', async () => {
            harness.clearEvents();
            const response = { command: 'stepIn', success: true } as DebugProtocol.StepInResponse;
            (harness.session as any).stepInRequest(response, { threadId: 1 });
            await new Promise(r => setTimeout(r, 100));

            assert.strictEqual((harness.session as any).currentSeq, 12);
            const stopped = harness.events.find(e => e.type === 'stopped');
            assert.ok(stopped);
            assert.strictEqual(stopped!.body.reason, 'step');
        });

        it('should step_back and update currentSeq', async () => {
            harness.clearEvents();
            const response = { command: 'stepBack', success: true } as DebugProtocol.StepBackResponse;
            (harness.session as any).stepBackRequest(response, { threadId: 1 });
            await new Promise(r => setTimeout(r, 100));

            assert.strictEqual((harness.session as any).currentSeq, 9);
        });

        it('should reverse_continue and emit start', async () => {
            harness.clearEvents();
            const response = { command: 'reverseContinue', success: true } as DebugProtocol.ReverseContinueResponse;
            (harness.session as any).reverseContinueRequest(response, { threadId: 1 });
            await new Promise(r => setTimeout(r, 100));

            assert.strictEqual((harness.session as any).currentSeq, 1);
            const stopped = harness.events.find(e => e.type === 'stopped');
            assert.ok(stopped);
            // 'start' maps to step event with description
            assert.strictEqual(stopped!.body.reason, 'step');
            assert.ok(stopped!.body.description?.includes('Beginning'));
        });

        it('should handle goto_targets request', async () => {
            const response = { command: 'gotoTargets', success: true } as DebugProtocol.GotoTargetsResponse;
            (harness.session as any).gotoTargetsRequest(response, {
                source: { path: '/test/script.py' },
                line: 5,
            });
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('gotoTargets');
            assert.ok(last?.success);
            assert.strictEqual(last?.body?.targets?.length, 1);
            assert.strictEqual(last?.body?.targets[0].id, 10);
        });

        it('should handle goto request', async () => {
            harness.clearEvents();
            const response = { command: 'goto', success: true } as DebugProtocol.GotoResponse;
            (harness.session as any).gotoRequest(response, { threadId: 1, targetId: 25 });
            await new Promise(r => setTimeout(r, 100));

            assert.strictEqual((harness.session as any).currentSeq, 25);
            const stopped = harness.events.find(e => e.type === 'stopped');
            assert.ok(stopped);
            assert.strictEqual(stopped!.body.reason, 'goto');
        });

        it('should handle restart_frame', async () => {
            harness.clearEvents();
            const response = { command: 'restartFrame', success: true } as DebugProtocol.RestartFrameResponse;
            (harness.session as any).restartFrameRequest(response, { frameId: 10 });
            await new Promise(r => setTimeout(r, 100));

            assert.strictEqual((harness.session as any).currentSeq, 5);
        });

        it('should merge breakpoints across files', async () => {
            // Set breakpoints for file1
            const resp1 = { command: 'setBreakpoints', success: true } as DebugProtocol.SetBreakpointsResponse;
            (harness.session as any).setBreakPointsRequest(resp1, {
                source: { path: '/test/a.py' },
                breakpoints: [{ line: 5 }, { line: 10 }],
            });
            await new Promise(r => setTimeout(r, 50));

            // Set breakpoints for file2
            const resp2 = { command: 'setBreakpoints', success: true } as DebugProtocol.SetBreakpointsResponse;
            (harness.session as any).setBreakPointsRequest(resp2, {
                source: { path: '/test/b.py' },
                breakpoints: [{ line: 3 }],
            });
            await new Promise(r => setTimeout(r, 50));

            // Verify the last set_breakpoints call sent all breakpoints
            const bpReqs = mockServer.receivedRequests.filter(r => r.method === 'set_breakpoints');
            const last = bpReqs[bpReqs.length - 1];
            assert.strictEqual(last.params.breakpoints.length, 3);

            // Response should include verified breakpoints for the current file
            assert.strictEqual(resp2.body.breakpoints.length, 1);
            assert.strictEqual(resp2.body.breakpoints[0].verified, true);
        });

        it('should forward condition field in breakpoints', async () => {
            const resp = { command: 'setBreakpoints', success: true } as DebugProtocol.SetBreakpointsResponse;
            (harness.session as any).setBreakPointsRequest(resp, {
                source: { path: '/test/script.py' },
                breakpoints: [{ line: 10, condition: 'x > 5' }],
            });
            await new Promise(r => setTimeout(r, 50));

            const bpReqs = mockServer.receivedRequests.filter(r => r.method === 'set_breakpoints');
            const last = bpReqs[bpReqs.length - 1];
            assert.strictEqual(last.params.breakpoints.length, 1);
            assert.strictEqual(last.params.breakpoints[0].condition, 'x > 5');
        });

        it('should omit condition field when not set', async () => {
            const resp = { command: 'setBreakpoints', success: true } as DebugProtocol.SetBreakpointsResponse;
            (harness.session as any).setBreakPointsRequest(resp, {
                source: { path: '/test/script.py' },
                breakpoints: [{ line: 10 }],
            });
            await new Promise(r => setTimeout(r, 50));

            const bpReqs = mockServer.receivedRequests.filter(r => r.method === 'set_breakpoints');
            const last = bpReqs[bpReqs.length - 1];
            assert.strictEqual(last.params.breakpoints[0].condition, undefined);
        });

        it('should handle notifications from backend', async () => {
            harness.clearEvents();
            (harness.session as any).isReplaying = false;

            // Register notification handler through the backend
            (harness.session as any).backend.onNotification((method: string, params: any) => {
                (harness.session as any).handleNotification(method, params);
            });

            // Backend sends 'stopped' notification
            mockServer.sendNotification('stopped', {
                seq: 0,
                totalFrames: 500,
                thread_id: 1,
            });
            await new Promise(r => setTimeout(r, 100));

            assert.strictEqual((harness.session as any).isReplaying, true);
            assert.strictEqual((harness.session as any).currentSeq, 0);
            assert.strictEqual((harness.session as any).totalFrames, 500);

            const stopped = harness.events.find(e => e.type === 'stopped');
            assert.ok(stopped, 'should emit stopped event from notification');
        });

        it('should handle output notification', async () => {
            harness.clearEvents();

            (harness.session as any).handleNotification('output', {
                output: 'Hello, world!\n',
                category: 'stdout',
            });

            const output = harness.events.find(e => e.type === 'output');
            assert.ok(output);
            assert.strictEqual(output!.body.output, 'Hello, world!\n');
            assert.strictEqual(output!.body.category, 'stdout');
        });

        it('should handle disconnect', async () => {
            const response = { command: 'disconnect', success: true } as DebugProtocol.DisconnectResponse;
            (harness.session as any).disconnectRequest(response, {});
            await new Promise(r => setTimeout(r, 100));

            const last = harness.lastResponse('disconnect');
            assert.ok(last);
        });

        describe('sendStoppedForReason', () => {
            const reasonTests: Array<[string, string, string?]> = [
                ['breakpoint', 'breakpoint'],
                ['exception', 'exception'],
                ['end', 'step', 'End of recording'],
                ['start', 'step', 'Beginning of recording'],
                ['goto', 'goto'],
                ['step', 'step'],
                ['unknown_reason', 'step'],
            ];

            for (const [reason, expectedStopReason, expectedDescription] of reasonTests) {
                it(`maps '${reason}' to '${expectedStopReason}' stopped event`, () => {
                    harness.clearEvents();
                    (harness.session as any).sendStoppedForReason(reason, { seq: 1 });

                    const stopped = harness.events.find(e => e.type === 'stopped');
                    assert.ok(stopped, `should emit stopped for reason '${reason}'`);
                    assert.strictEqual(stopped!.body.reason, expectedStopReason);
                    if (expectedDescription) {
                        assert.ok(stopped!.body.description?.includes(expectedDescription),
                            `description should include '${expectedDescription}'`);
                    }
                });
            }

            it('emits positionChanged event with navigation result', () => {
                harness.clearEvents();
                (harness.session as any).sendStoppedForReason('step', {
                    seq: 42, file: '/test/script.py', line: 10
                });

                const posEvent = harness.events.find(e => e.type === 'pyttd/positionChanged');
                assert.ok(posEvent, 'should emit positionChanged');
                assert.strictEqual(posEvent!.body.seq, 42);
                assert.strictEqual(posEvent!.body.file, '/test/script.py');
            });

            it('uses thread_id from navigation result', () => {
                harness.clearEvents();
                (harness.session as any).sendStoppedForReason('step', {
                    seq: 1, thread_id: 12345
                });

                const stopped = harness.events.find(e => e.type === 'stopped');
                assert.strictEqual(stopped!.body.threadId, 12345);
            });

            it('defaults thread_id to 1 when missing', () => {
                harness.clearEvents();
                (harness.session as any).sendStoppedForReason('step', { seq: 1 });

                const stopped = harness.events.find(e => e.type === 'stopped');
                assert.strictEqual(stopped!.body.threadId, 1);
            });
        });

        describe('customRequest', () => {
            it('should handle goto_frame as navigation', async () => {
                harness.clearEvents();
                const response = { command: 'goto_frame', success: true } as DebugProtocol.Response;
                (harness.session as any).customRequest('goto_frame', response, { target_seq: 30 });
                await new Promise(r => setTimeout(r, 100));

                assert.strictEqual((harness.session as any).currentSeq, 30);
                const stopped = harness.events.find(e => e.type === 'stopped');
                assert.ok(stopped);
            });

            it('should pass through query requests', async () => {
                mockServer.handlers.set('get_traced_files', () => ({
                    files: ['/test/script.py']
                }));

                const response = { command: 'get_traced_files', success: true } as DebugProtocol.Response;
                (harness.session as any).customRequest('get_traced_files', response, {});
                await new Promise(r => setTimeout(r, 100));

                const last = harness.lastResponse('get_traced_files');
                assert.ok(last?.success);
                assert.deepStrictEqual(last?.body?.files, ['/test/script.py']);
            });
        });

        describe('variable expansion', () => {
            it('should assign non-zero variablesReference for expandable variables', async () => {
                // Override get_variables to return an expandable variable
                mockServer.handlers.set('get_variables', () => ({
                    variables: [
                        { name: 'x', value: '42', type: 'int', variablesReference: 0 },
                        { name: 'data', value: "{'a': 1}", type: 'dict', variablesReference: 1000 },
                    ]
                }));

                const response = { command: 'variables', success: true } as DebugProtocol.VariablesResponse;
                (harness.session as any).variablesRequest(response, { variablesReference: 11 });
                await new Promise(r => setTimeout(r, 100));

                const last = harness.lastResponse('variables');
                assert.ok(last?.success);
                assert.strictEqual(last?.body?.variables?.length, 2);
                // x should have ref 0 (not expandable)
                assert.strictEqual(last?.body?.variables[0].variablesReference, 0);
                // data should have a non-zero ref (expandable)
                assert.ok(last?.body?.variables[1].variablesReference > 0,
                    'expandable variable should have non-zero variablesReference');
            });

            it('should call get_variable_children for child expansion', async () => {
                // First, set up expandable variables
                mockServer.handlers.set('get_variables', () => ({
                    variables: [
                        { name: 'data', value: "{'a': 1}", type: 'dict', variablesReference: 1000 },
                    ]
                }));
                mockServer.handlers.set('get_variable_children', (params: any) => ({
                    variables: [
                        { name: "'a'", value: '1', type: 'int', variablesReference: 0 },
                    ]
                }));

                // Request scope variables first to populate childRefMap
                const resp1 = { command: 'variables', success: true } as DebugProtocol.VariablesResponse;
                (harness.session as any).variablesRequest(resp1, { variablesReference: 11 });
                await new Promise(r => setTimeout(r, 100));

                const vars = harness.lastResponse('variables');
                const dataRef = vars?.body?.variables[0].variablesReference;
                assert.ok(dataRef > 0, 'should have non-zero ref');

                // Now request children using that ref
                harness.clearResponses();
                const resp2 = { command: 'variables', success: true } as DebugProtocol.VariablesResponse;
                (harness.session as any).variablesRequest(resp2, { variablesReference: dataRef });
                await new Promise(r => setTimeout(r, 100));

                const children = harness.lastResponse('variables');
                assert.ok(children?.success);
                assert.strictEqual(children?.body?.variables?.length, 1);
                assert.strictEqual(children?.body?.variables[0].name, "'a'");
                assert.strictEqual(children?.body?.variables[0].value, '1');
                // Children should have ref 0 (flat, one level)
                assert.strictEqual(children?.body?.variables[0].variablesReference, 0);

                // Verify the backend received get_variable_children
                const childReq = mockServer.receivedRequests.find(
                    r => r.method === 'get_variable_children'
                );
                assert.ok(childReq, 'should call get_variable_children');
                assert.strictEqual(childReq!.params.variablesReference, 1000);
            });

            it('should clear childRefMap on navigation', async () => {
                // Set up an expandable variable
                mockServer.handlers.set('get_variables', () => ({
                    variables: [
                        { name: 'data', value: "{'a': 1}", type: 'dict', variablesReference: 1000 },
                    ]
                }));

                // Populate childRefMap
                const resp1 = { command: 'variables', success: true } as DebugProtocol.VariablesResponse;
                (harness.session as any).variablesRequest(resp1, { variablesReference: 11 });
                await new Promise(r => setTimeout(r, 100));

                const vars = harness.lastResponse('variables');
                const dataRef = vars?.body?.variables[0].variablesReference;
                assert.ok(dataRef > 0);

                // Simulate navigation (clears childRefMap)
                (harness.session as any).sendStoppedForReason('step', { seq: 20 });

                // Now the old ref should not be in childRefMap
                // Requesting with it should treat it as scope request
                const childRefMap = (harness.session as any).childRefMap as Map<number, number>;
                assert.strictEqual(childRefMap.size, 0, 'childRefMap should be cleared after navigation');
            });
        });
    });
});
