import * as net from 'net';
import * as child_process from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

class JsonRpcConnection {
    private buffer: Buffer = Buffer.alloc(0);
    private pendingRequests = new Map<number, { resolve: (value: any) => void; reject: (reason: any) => void; timer: NodeJS.Timeout }>();
    private nextId = 1;
    private notificationCallback: ((method: string, params: any) => void) | null = null;
    private rpcTimeout: number;

    constructor(private socket: net.Socket, rpcTimeout: number = 5000) {
        this.rpcTimeout = rpcTimeout;
        socket.on('data', (data: Buffer) => {
            this.buffer = Buffer.concat([this.buffer, data]);
            this.processBuffer();
        });
        socket.on('close', () => {
            for (const [id, pending] of this.pendingRequests) {
                clearTimeout(pending.timer);
                pending.reject(new Error('Connection closed'));
            }
            this.pendingRequests.clear();
        });
    }

    private processBuffer(): void {
        while (true) {
            const headerEnd = this.buffer.indexOf('\r\n\r\n');
            if (headerEnd < 0) break;

            const header = this.buffer.subarray(0, headerEnd).toString('ascii');
            let contentLength: number | null = null;
            for (const line of header.split('\r\n')) {
                if (line.toLowerCase().startsWith('content-length:')) {
                    contentLength = parseInt(line.split(':')[1].trim(), 10);
                }
            }
            if (contentLength === null) break;

            const bodyStart = headerEnd + 4;
            const bodyEnd = bodyStart + contentLength;
            if (this.buffer.length < bodyEnd) break;

            const body = this.buffer.subarray(bodyStart, bodyEnd).toString('utf-8');
            this.buffer = this.buffer.subarray(bodyEnd);

            try {
                const msg = JSON.parse(body);
                this.handleMessage(msg);
            } catch (e) {
                // Skip malformed messages
            }
        }
    }

    private handleMessage(msg: any): void {
        if ('id' in msg && ('result' in msg || 'error' in msg)) {
            // Response
            const pending = this.pendingRequests.get(msg.id);
            if (pending) {
                clearTimeout(pending.timer);
                this.pendingRequests.delete(msg.id);
                if (msg.error) {
                    pending.reject(new Error(msg.error.message));
                } else {
                    pending.resolve(msg.result);
                }
            }
        } else if ('method' in msg && !('id' in msg)) {
            // Notification
            if (this.notificationCallback) {
                this.notificationCallback(msg.method, msg.params || {});
            }
        }
    }

    sendRequest(method: string, params: any = {}): Promise<any> {
        return new Promise((resolve, reject) => {
            const id = this.nextId++;
            const timer = setTimeout(() => {
                this.pendingRequests.delete(id);
                reject(new Error(
                    `RPC timeout after ${this.rpcTimeout}ms calling '${method}'. ` +
                    `The backend may be overloaded. Try increasing rpcTimeout in launch.json.`
                ));
            }, this.rpcTimeout);
            this.pendingRequests.set(id, { resolve, reject, timer });

            const msg = JSON.stringify({ jsonrpc: '2.0', id, method, params });
            const body = Buffer.from(msg, 'utf-8');
            const header = `Content-Length: ${body.length}\r\n\r\n`;
            this.socket.write(header + msg);
        });
    }

    onNotification(callback: (method: string, params: any) => void): void {
        this.notificationCallback = callback;
    }

    close(): void {
        for (const [id, pending] of this.pendingRequests) {
            clearTimeout(pending.timer);
        }
        this.pendingRequests.clear();
        this.socket.destroy();
    }
}

export class BackendConnection {
    private process: child_process.ChildProcess | null = null;
    private socket: net.Socket | null = null;
    private rpc: JsonRpcConnection | null = null;
    private notificationCallback: ((method: string, params: any) => void) | null = null;
    private exitCallback: ((code: number | null) => void) | null = null;

    async spawn(pythonPath: string, args: string[], env?: { [key: string]: string }): Promise<number> {
        const spawnEnv = env ? { ...process.env, ...env } : process.env;
        this.process = child_process.spawn(pythonPath, ['-m', 'pyttd', 'serve', ...args], {
            stdio: ['pipe', 'pipe', 'pipe'],
            env: spawnEnv as NodeJS.ProcessEnv,
        });

        const port = await new Promise<number>((resolve, reject) => {
            const timeout = setTimeout(() => {
                reject(new Error('Timeout waiting for backend port'));
            }, 10000);

            let resolved = false;

            let stdoutBuffer = '';
            this.process!.stdout!.on('data', (data: Buffer) => {
                stdoutBuffer += data.toString();
                const lines = stdoutBuffer.split('\n');
                stdoutBuffer = lines.pop() || '';
                for (const line of lines) {
                    const match = line.trim().match(/^PYTTD_PORT:(\d+)$/);
                    if (match) {
                        clearTimeout(timeout);
                        resolved = true;
                        resolve(parseInt(match[1], 10));
                        return;
                    }
                }
            });

            let stderrBuffer = '';
            this.process!.stderr!.on('data', (data: Buffer) => {
                stderrBuffer += data.toString();
            });

            this.process!.on('exit', (code) => {
                if (!resolved) {
                    clearTimeout(timeout);
                    reject(new Error(`Backend exited with code ${code}: ${stderrBuffer}`));
                }
            });

            this.process!.on('error', (err) => {
                if (!resolved) {
                    clearTimeout(timeout);
                    reject(new Error(`Failed to spawn backend: ${err.message}`));
                }
            });
        });

        // Install persistent exit listener for post-handshake crash detection
        this.process!.on('exit', (code) => {
            if (this.exitCallback) {
                this.exitCallback(code);
            }
        });

        return port;
    }

    onExit(callback: (code: number | null) => void): void {
        this.exitCallback = callback;
    }

    async connect(port: number, rpcTimeout: number = 5000): Promise<void> {
        return new Promise<void>((resolve, reject) => {
            const socket = new net.Socket();
            const timeout = setTimeout(() => {
                socket.destroy();
                reject(new Error('Timeout connecting to backend'));
            }, 5000);

            socket.connect(port, '127.0.0.1', () => {
                clearTimeout(timeout);
                this.socket = socket;
                this.rpc = new JsonRpcConnection(socket, rpcTimeout);
                if (this.notificationCallback) {
                    this.rpc.onNotification(this.notificationCallback);
                }
                resolve();
            });

            socket.on('error', (err) => {
                clearTimeout(timeout);
                reject(new Error(`Failed to connect to backend: ${err.message}`));
            });
        });
    }

    async sendRequest(method: string, params: any = {}): Promise<any> {
        if (!this.rpc) {
            throw new Error('Not connected to backend');
        }
        return this.rpc.sendRequest(method, params);
    }

    onNotification(callback: (method: string, params: any) => void): void {
        this.notificationCallback = callback;
        if (this.rpc) {
            this.rpc.onNotification(callback);
        }
    }

    close(): void {
        this.exitCallback = null;
        if (this.rpc) {
            this.rpc.close();
            this.rpc = null;
        }
        if (this.socket) {
            this.socket.destroy();
            this.socket = null;
        }
        if (this.process) {
            this.process.kill();
            this.process = null;
        }
    }
}

export function findPythonPath(launchConfig: any, workspaceRoot: string): string {
    // 1. pythonPath from launch config
    if (launchConfig.pythonPath) {
        return launchConfig.pythonPath;
    }

    // 2. VSCode Python extension's configured interpreter
    try {
        const vscode = require('vscode');
        const pythonConfig = vscode.workspace.getConfiguration('python');
        const defaultPath: string | undefined = pythonConfig.get('defaultInterpreterPath');
        if (defaultPath && defaultPath !== 'python') {
            const resolved = defaultPath.replace(/\$\{workspaceFolder\}/g, workspaceRoot);
            if (fs.existsSync(resolved)) {
                return resolved;
            }
        }
    } catch {
        // vscode module not available (running in tests or standalone)
    }

    // 3. Common venv paths
    for (const venvDir of ['.venv', 'venv']) {
        const candidate = path.join(workspaceRoot, venvDir, 'bin', 'python');
        if (fs.existsSync(candidate)) {
            return candidate;
        }
    }

    // 3. python3 or python on PATH
    try {
        child_process.execFileSync('python3', ['--version'], { stdio: 'pipe' });
        return 'python3';
    } catch {
        // fall through
    }

    try {
        child_process.execFileSync('python', ['--version'], { stdio: 'pipe' });
        return 'python';
    } catch {
        // fall through
    }

    throw new Error(
        "Could not find a Python interpreter. Set 'pythonPath' in your launch configuration."
    );
}
