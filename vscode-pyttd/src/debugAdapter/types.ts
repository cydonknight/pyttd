export interface JsonRpcRequest {
    jsonrpc: '2.0';
    id: number;
    method: string;
    params?: any;
}

export interface JsonRpcResponse {
    jsonrpc: '2.0';
    id: number;
    result?: any;
    error?: { code: number; message: string };
}

export interface JsonRpcNotification {
    jsonrpc: '2.0';
    method: string;
    params?: any;
}

export interface PyttdLaunchConfig {
    type: 'pyttd';
    request: 'launch';
    program?: string;
    module?: string;
    pythonPath?: string;
    cwd?: string;
    args?: string[];
    traceDb?: string;
    checkpointInterval?: number;
    rpcTimeout?: number;
    env?: { [key: string]: string };
    envFile?: string;
    stopOnEntry?: boolean;
    maxFrames?: number;
    console?: 'integratedTerminal' | 'internalConsole';
}
