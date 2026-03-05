import {
    LoggingDebugSession,
    InitializedEvent,
    StoppedEvent,
    Thread,
    StackFrame,
    Scope,
    Source,
    Variable,
    TerminatedEvent,
} from '@vscode/debugadapter';
import { DebugProtocol } from '@vscode/debugprotocol';
import { BackendConnection } from './backendConnection';
import { PyttdLaunchConfig } from './types';

export class PyttdDebugSession extends LoggingDebugSession {
    private backend: BackendConnection = new BackendConnection();

    public constructor() {
        super();
        this.setDebuggerLinesStartAt1(true);
        this.setDebuggerColumnsStartAt1(true);
    }

    protected initializeRequest(response: DebugProtocol.InitializeResponse, args: DebugProtocol.InitializeRequestArguments): void {
        response.body = response.body || {};
        response.body.supportsConfigurationDoneRequest = true;
        response.body.supportsEvaluateForHovers = true;
        this.sendResponse(response);
    }

    protected launchRequest(response: DebugProtocol.LaunchResponse, args: DebugProtocol.LaunchRequestArguments): void {
        // Phase 3: spawn backend, connect TCP, send InitializedEvent
        this.sendResponse(response);
    }

    protected setBreakPointsRequest(response: DebugProtocol.SetBreakpointsResponse, args: DebugProtocol.SetBreakpointsArguments): void {
        // Phase 3: forward breakpoints to backend
        response.body = { breakpoints: [] };
        this.sendResponse(response);
    }

    protected configurationDoneRequest(response: DebugProtocol.ConfigurationDoneResponse, args: DebugProtocol.ConfigurationDoneArguments): void {
        // Phase 3: send configuration_done RPC to start recording
        this.sendResponse(response);
    }

    protected threadsRequest(response: DebugProtocol.ThreadsResponse): void {
        response.body = { threads: [new Thread(1, "Main Thread")] };
        this.sendResponse(response);
    }

    protected stackTraceRequest(response: DebugProtocol.StackTraceResponse, args: DebugProtocol.StackTraceArguments): void {
        // Phase 3: query backend for stack trace
        response.body = { stackFrames: [], totalFrames: 0 };
        this.sendResponse(response);
    }

    protected scopesRequest(response: DebugProtocol.ScopesResponse, args: DebugProtocol.ScopesArguments): void {
        // Phase 3: return Locals scope
        response.body = { scopes: [] };
        this.sendResponse(response);
    }

    protected variablesRequest(response: DebugProtocol.VariablesResponse, args: DebugProtocol.VariablesArguments): void {
        // Phase 3: query backend for variables
        response.body = { variables: [] };
        this.sendResponse(response);
    }

    protected evaluateRequest(response: DebugProtocol.EvaluateResponse, args: DebugProtocol.EvaluateArguments): void {
        // Phase 3: lookup variable in recorded state
        response.body = { result: '', variablesReference: 0 };
        this.sendResponse(response);
    }

    protected continueRequest(response: DebugProtocol.ContinueResponse, args: DebugProtocol.ContinueArguments): void {
        // Phase 3: forward continue to backend
        this.sendResponse(response);
    }

    protected nextRequest(response: DebugProtocol.NextResponse, args: DebugProtocol.NextArguments): void {
        // Phase 3: step over
        this.sendResponse(response);
    }

    protected stepInRequest(response: DebugProtocol.StepInResponse, args: DebugProtocol.StepInArguments): void {
        // Phase 3: step into
        this.sendResponse(response);
    }

    protected stepOutRequest(response: DebugProtocol.StepOutResponse, args: DebugProtocol.StepOutArguments): void {
        // Phase 3: step out
        this.sendResponse(response);
    }

    protected pauseRequest(response: DebugProtocol.PauseResponse, args: DebugProtocol.PauseArguments): void {
        // Phase 3: interrupt recording
        this.sendResponse(response);
    }

    protected disconnectRequest(response: DebugProtocol.DisconnectResponse, args: DebugProtocol.DisconnectArguments): void {
        // Phase 3: shutdown backend, cleanup
        this.backend.close();
        this.sendResponse(response);
    }

    // Phase 4: stepBackRequest, reverseContinueRequest, gotoTargetsRequest, gotoRequest, restartFrameRequest
}
