import * as net from 'net';
import * as child_process from 'child_process';

export class BackendConnection {
    private process: child_process.ChildProcess | null = null;
    private socket: net.Socket | null = null;

    async spawn(pythonPath: string, args: string[]): Promise<number> {
        // Phase 3: spawn child process, read PYTTD_PORT:<port> from stdout
        throw new Error('Not yet implemented');
    }

    async connect(port: number): Promise<void> {
        // Phase 3: TCP connect, wrap in JSON-RPC connection
        throw new Error('Not yet implemented');
    }

    async sendRequest(method: string, params: any): Promise<any> {
        // Phase 3: send JSON-RPC request, await response
        throw new Error('Not yet implemented');
    }

    onNotification(callback: (method: string, params: any) => void): void {
        // Phase 3: register notification handler
    }

    close(): void {
        if (this.socket) { this.socket.destroy(); this.socket = null; }
        if (this.process) { this.process.kill(); this.process = null; }
    }
}
