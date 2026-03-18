import * as assert from 'assert';
import * as net from 'net';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';
import { BackendConnection, findPythonPath } from '../debugAdapter/backendConnection';

/** Wait for a condition to become true, polling every 5ms. */
function waitFor(condition: () => boolean, timeout = 1000): Promise<void> {
    return new Promise((resolve, reject) => {
        const deadline = Date.now() + timeout;
        const check = () => {
            if (condition()) return resolve();
            if (Date.now() > deadline) return reject(new Error('waitFor timeout'));
            setTimeout(check, 5);
        };
        check();
    });
}

describe('BackendConnection', () => {
    let server: net.Server;
    let serverPort: number;
    let serverSocket: net.Socket | null;

    beforeEach((done) => {
        serverSocket = null;
        server = net.createServer((socket) => {
            serverSocket = socket;
        });
        server.listen(0, '127.0.0.1', () => {
            serverPort = (server.address() as net.AddressInfo).port;
            done();
        });
    });

    afterEach((done) => {
        if (serverSocket) serverSocket.destroy();
        server.close(done);
    });

    describe('connect and sendRequest', () => {
        it('should connect to a TCP server', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);
            assert.ok(serverSocket, 'server should have accepted connection');
            conn.close();
        });

        it('should send a JSON-RPC request and receive response', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);

            // Set up server to respond
            serverSocket!.on('data', (data: Buffer) => {
                const str = data.toString();
                const bodyStart = str.indexOf('\r\n\r\n') + 4;
                const msg = JSON.parse(str.substring(bodyStart));

                const response = JSON.stringify({
                    jsonrpc: '2.0',
                    id: msg.id,
                    result: { status: 'ok', value: 42 },
                });
                const header = `Content-Length: ${Buffer.byteLength(response)}\r\n\r\n`;
                serverSocket!.write(header + response);
            });

            const result = await conn.sendRequest('test_method', { foo: 'bar' });
            assert.deepStrictEqual(result, { status: 'ok', value: 42 });
            conn.close();
        });

        it('should reject on RPC error response', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);

            serverSocket!.on('data', (data: Buffer) => {
                const str = data.toString();
                const bodyStart = str.indexOf('\r\n\r\n') + 4;
                const msg = JSON.parse(str.substring(bodyStart));

                const response = JSON.stringify({
                    jsonrpc: '2.0',
                    id: msg.id,
                    error: { code: -32600, message: 'Invalid request' },
                });
                const header = `Content-Length: ${Buffer.byteLength(response)}\r\n\r\n`;
                serverSocket!.write(header + response);
            });

            await assert.rejects(
                () => conn.sendRequest('bad_method'),
                /Invalid request/
            );
            conn.close();
        });

        it('should reject on timeout', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort, 100); // 100ms timeout

            // Server does NOT respond — timeout should trigger
            await assert.rejects(
                () => conn.sendRequest('slow_method'),
                /RPC timeout/
            );
            conn.close();
        });

        it('should throw when not connected', async () => {
            const conn = new BackendConnection();
            await assert.rejects(
                () => conn.sendRequest('test'),
                /Not connected/
            );
        });

        it('should receive notifications', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);

            const notifications: Array<{ method: string; params: any }> = [];
            conn.onNotification((method, params) => {
                notifications.push({ method, params });
            });

            // Server sends a notification (no id)
            const notification = JSON.stringify({
                jsonrpc: '2.0',
                method: 'stopped',
                params: { seq: 100, reason: 'step' },
            });
            const header = `Content-Length: ${Buffer.byteLength(notification)}\r\n\r\n`;
            serverSocket!.write(header + notification);

            // Wait for event
            await new Promise(resolve => setTimeout(resolve, 50));

            assert.strictEqual(notifications.length, 1);
            assert.strictEqual(notifications[0].method, 'stopped');
            assert.strictEqual(notifications[0].params.seq, 100);
            conn.close();
        });

        it('should handle multiple messages in single TCP chunk', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);

            const notifications: string[] = [];
            conn.onNotification((method) => {
                notifications.push(method);
            });

            // Send two notifications in one write
            const n1 = JSON.stringify({ jsonrpc: '2.0', method: 'event1', params: {} });
            const n2 = JSON.stringify({ jsonrpc: '2.0', method: 'event2', params: {} });
            const chunk = `Content-Length: ${Buffer.byteLength(n1)}\r\n\r\n${n1}` +
                          `Content-Length: ${Buffer.byteLength(n2)}\r\n\r\n${n2}`;
            serverSocket!.write(chunk);

            await new Promise(resolve => setTimeout(resolve, 50));

            assert.strictEqual(notifications.length, 2);
            assert.strictEqual(notifications[0], 'event1');
            assert.strictEqual(notifications[1], 'event2');
            conn.close();
        });

        it('should handle message split across TCP chunks', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);

            const notifications: string[] = [];
            conn.onNotification((method) => {
                notifications.push(method);
            });

            const body = JSON.stringify({ jsonrpc: '2.0', method: 'split_test', params: {} });
            const full = `Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`;

            // Split in the middle
            const mid = Math.floor(full.length / 2);
            serverSocket!.write(full.substring(0, mid));
            await new Promise(resolve => setTimeout(resolve, 20));
            serverSocket!.write(full.substring(mid));

            await new Promise(resolve => setTimeout(resolve, 50));

            assert.strictEqual(notifications.length, 1);
            assert.strictEqual(notifications[0], 'split_test');
            conn.close();
        });

        it('should reject pending requests on connection close', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort, 5000);
            await waitFor(() => serverSocket !== null);

            const promise = conn.sendRequest('will_disconnect');

            // Server closes connection
            serverSocket!.destroy();

            await assert.rejects(() => promise, /Connection closed/);
            conn.close();
        });
    });

    describe('close', () => {
        it('should be idempotent', async () => {
            const conn = new BackendConnection();
            await conn.connect(serverPort);
            conn.close();
            conn.close(); // Should not throw
        });

        it('should be safe before connect', () => {
            const conn = new BackendConnection();
            conn.close(); // Should not throw
        });
    });

    describe('onNotification before connect', () => {
        it('should register callback that fires after connect', async () => {
            const conn = new BackendConnection();
            const notifications: string[] = [];
            conn.onNotification((method) => {
                notifications.push(method);
            });

            await conn.connect(serverPort);
            await waitFor(() => serverSocket !== null);

            const body = JSON.stringify({ jsonrpc: '2.0', method: 'late_bind', params: {} });
            serverSocket!.write(`Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);

            await new Promise(resolve => setTimeout(resolve, 50));
            assert.strictEqual(notifications.length, 1);
            assert.strictEqual(notifications[0], 'late_bind');
            conn.close();
        });
    });
});

describe('findPythonPath', () => {
    let tmpDir: string;

    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pyttd-findpython-'));
    });

    afterEach(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('should return pythonPath from launch config if set', () => {
        const result = findPythonPath({ pythonPath: '/usr/bin/python3.12' }, tmpDir);
        assert.strictEqual(result, '/usr/bin/python3.12');
    });

    it('should find .venv/bin/python', () => {
        const venvPython = path.join(tmpDir, '.venv', 'bin', 'python');
        fs.mkdirSync(path.dirname(venvPython), { recursive: true });
        fs.writeFileSync(venvPython, '');
        const result = findPythonPath({}, tmpDir);
        assert.strictEqual(result, venvPython);
    });

    it('should find venv/bin/python', () => {
        const venvPython = path.join(tmpDir, 'venv', 'bin', 'python');
        fs.mkdirSync(path.dirname(venvPython), { recursive: true });
        fs.writeFileSync(venvPython, '');
        const result = findPythonPath({}, tmpDir);
        assert.strictEqual(result, venvPython);
    });

    it('should prefer .venv over venv', () => {
        const dotVenv = path.join(tmpDir, '.venv', 'bin', 'python');
        const venv = path.join(tmpDir, 'venv', 'bin', 'python');
        fs.mkdirSync(path.dirname(dotVenv), { recursive: true });
        fs.writeFileSync(dotVenv, '');
        fs.mkdirSync(path.dirname(venv), { recursive: true });
        fs.writeFileSync(venv, '');
        const result = findPythonPath({}, tmpDir);
        assert.strictEqual(result, dotVenv);
    });

    it('should fall back to python3 or python on PATH when no venv', () => {
        // No venvs in tmpDir, should find python3 or python on system PATH
        const result = findPythonPath({}, tmpDir);
        assert.ok(
            result === 'python3' || result === 'python',
            `Expected 'python3' or 'python', got '${result}'`
        );
    });
});
