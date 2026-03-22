import {
    LoggingDebugSession,
    InitializedEvent,
    StoppedEvent,
    OutputEvent,
    TerminatedEvent,
    Thread,
    StackFrame,
    Scope,
    Source,
    Variable,
    ProgressStartEvent,
    ProgressUpdateEvent,
    ProgressEndEvent,
    Event,
} from '@vscode/debugadapter';
import { DebugProtocol } from '@vscode/debugprotocol';
import * as path from 'path';
import * as fs from 'fs';
import { BackendConnection, findPythonPath } from './backendConnection';
import { PyttdLaunchConfig } from './types';

function parseEnvFile(filePath: string): { [key: string]: string } {
    const result: { [key: string]: string } = {};
    if (!fs.existsSync(filePath)) return result;
    const content = fs.readFileSync(filePath, 'utf-8');
    for (const line of content.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const eqIdx = trimmed.indexOf('=');
        if (eqIdx < 0) continue;
        const key = trimmed.substring(0, eqIdx).trim();
        let value = trimmed.substring(eqIdx + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'"))) {
            value = value.substring(1, value.length - 1);
        }
        result[key] = value;
    }
    return result;
}

export class PyttdDebugSession extends LoggingDebugSession {
    private backend: BackendConnection = new BackendConnection();
    private isReplaying = false;
    private currentSeq = 0;
    private totalFrames = 0;
    private timelineStartSeq: number | null = null;
    private timelineEndSeq: number | null = null;
    private breakpointsByFile = new Map<string, DebugProtocol.SourceBreakpoint[]>();
    private functionBreakpoints: Array<{ name: string; condition?: string; hitCondition?: string }> = [];
    private dataBreakpoints: Array<{ name: string; dataId: string; accessType?: string }> = [];
    private readonly progressId = 'pyttd-recording';
    private nextVarRef = 0x4000_0000;
    private childRefMap = new Map<number, number>();
    private stopOnEntry = true;
    private droppedFrameWarningShown = false;

    public constructor() {
        super();
        this.setDebuggerLinesStartAt1(true);
        this.setDebuggerColumnsStartAt1(true);
    }

    protected initializeRequest(
        response: DebugProtocol.InitializeResponse,
        args: DebugProtocol.InitializeRequestArguments
    ): void {
        response.body = response.body || {};
        response.body.supportsConfigurationDoneRequest = true;
        response.body.supportsEvaluateForHovers = true;
        (response.body as any).supportsProgressReporting = true;
        response.body.supportsStepBack = true;
        response.body.supportsGotoTargetsRequest = true;
        response.body.supportsRestartFrame = true;
        response.body.supportsConditionalBreakpoints = true;
        response.body.supportsFunctionBreakpoints = true;
        response.body.supportsHitConditionalBreakpoints = true;
        response.body.supportsLogPoints = true;
        response.body.supportsDataBreakpoints = true;
        this.sendResponse(response);
    }

    protected launchRequest(
        response: DebugProtocol.LaunchResponse,
        args: DebugProtocol.LaunchRequestArguments
    ): void {
        const config = args as unknown as PyttdLaunchConfig;
        const workspaceRoot = config.cwd || '.';

        this.stopOnEntry = config.stopOnEntry !== false; // default true

        if (config.console === 'integratedTerminal') {
            this.sendEvent(new OutputEvent(
                'Warning: integratedTerminal console is not yet supported. Using internalConsole.\n',
                'console'
            ));
        }

        let pythonPath: string;
        try {
            pythonPath = findPythonPath(config, workspaceRoot);
        } catch (e: any) {
            this.sendErrorResponse(response, 1,
                e.message + '\nHint: Install the VSCode Python extension and configure python.defaultInterpreterPath.');
            this.sendEvent(new TerminatedEvent());
            return;
        }

        const spawnArgs: string[] = [];
        if (config.module) {
            spawnArgs.push('--script', config.module, '--module');
        } else if (config.program) {
            const programPath = path.resolve(workspaceRoot, config.program);
            spawnArgs.push('--script', programPath);
        } else {
            this.sendErrorResponse(response, 1, "Launch config must specify 'program' or 'module'");
            this.sendEvent(new TerminatedEvent());
            return;
        }
        spawnArgs.push('--cwd', path.resolve(workspaceRoot));
        if (config.checkpointInterval !== undefined) {
            spawnArgs.push('--checkpoint-interval', String(config.checkpointInterval));
        }

        // Build environment from envFile + env
        let envVars: { [key: string]: string } = {};
        if (config.envFile) {
            const envFilePath = path.resolve(workspaceRoot, config.envFile);
            envVars = parseEnvFile(envFilePath);
        }
        if (config.env) {
            envVars = { ...envVars, ...config.env };
        }

        const rpcTimeout = config.rpcTimeout || 5000;

        this.backend
            .spawn(pythonPath, spawnArgs, Object.keys(envVars).length > 0 ? envVars : undefined)
            .then((port: number) => this.backend.connect(port, rpcTimeout))
            .then(() => {
                // Register notification handler
                this.backend.onNotification((method: string, params: any) => {
                    this.handleNotification(method, params);
                });

                // Monitor for unexpected backend exit
                this.backend.onExit((code) => {
                    this.sendEvent(new OutputEvent(
                        `Backend exited unexpectedly (code ${code})\n`, 'stderr'
                    ));
                    this.sendEvent(new Event('pyttd/error', {
                        message: `pyttd backend crashed (exit code ${code}).`,
                        detail: 'Check the Debug Console. Try running "pyttd record" from the terminal for details.',
                        severity: 'error',
                    }));
                    this.sendEvent(new TerminatedEvent());
                });

                return this.backend.sendRequest('backend_init');
            })
            .then(() => {
                const launchParams: any = {
                    args: config.args || [],
                };
                if (config.checkpointInterval !== undefined) {
                    launchParams.checkpointInterval = config.checkpointInterval;
                }
                if (config.traceDb) {
                    launchParams.traceDb = config.traceDb;
                }
                if (config.maxFrames) {
                    launchParams.maxFrames = config.maxFrames;
                }
                if (Object.keys(envVars).length > 0) {
                    launchParams.env = envVars;
                }
                return this.backend.sendRequest('launch', launchParams);
            })
            .then(() => {
                this.sendEvent(new InitializedEvent());
                this.sendResponse(response);
            })
            .catch((err: Error) => {
                this.backend.close();
                this.sendErrorResponse(response, 1, err.message);
                this.sendEvent(new TerminatedEvent());
            });
    }

    private handleNotification(method: string, params: any): void {
        switch (method) {
            case 'stopped':
                this.currentSeq = params.seq;
                this.totalFrames = params.totalFrames || 0;
                this.isReplaying = true;
                this.droppedFrameWarningShown = false;
                this.sendEvent(new ProgressEndEvent(this.progressId));

                if (!this.stopOnEntry && params.reason === 'recording_complete') {
                    // Auto-continue to first breakpoint
                    this.backend.sendRequest('continue').then((result: any) => {
                        this.currentSeq = result.seq;
                        this.sendStoppedForReason(result.reason, result);
                    }).catch(() => {
                        this.sendEvent(new StoppedEvent('entry', params.thread_id || 1));
                    });
                } else {
                    this.sendEvent(new StoppedEvent('entry', params.thread_id || 1));
                }

                // Send initial position to timeline
                this.sendEvent(new Event('pyttd/positionChanged', {
                    seq: params.seq,
                }));
                // Request initial timeline data
                this.timelineStartSeq = 0;
                this.timelineEndSeq = this.totalFrames;
                this.backend.sendRequest('get_timeline_summary', {
                    startSeq: 0, endSeq: this.totalFrames, bucketCount: 500
                }).then((result: any) => {
                    this.sendEvent(new Event('pyttd/timelineData', {
                        buckets: result.buckets,
                        totalFrames: this.totalFrames,
                        startSeq: 0,
                        endSeq: this.totalFrames,
                    }));
                }).catch(() => {});
                break;
            case 'output':
                this.sendEvent(new OutputEvent(params.output, params.category));
                break;
            case 'progress': {
                const cpCount = params.checkpointCount ?? 0;
                const cpMB = params.checkpointMemoryMB ?? 0;
                let msg = `Recording: ${params.frameCount} frames`;
                if (cpCount > 0) {
                    msg += ` | ${cpCount} checkpoints (${cpMB} MB)`;
                }
                const warnings: string[] = [];
                if (params.droppedFrames > 0) warnings.push(`${params.droppedFrames} dropped`);
                if (params.poolOverflows > 0) warnings.push(`${params.poolOverflows} overflows`);
                if (warnings.length > 0) msg += ` (${warnings.join(', ')})`;
                this.sendEvent(new ProgressUpdateEvent(this.progressId, msg));
                // Emit checkpoint memory custom event for status bar
                if (cpCount > 0) {
                    this.sendEvent(new Event('pyttd/checkpointMemory', {
                        checkpointCount: cpCount,
                        checkpointMemoryMB: cpMB,
                    }));
                }
                // Emit custom event so extension.ts can update status bar
                this.sendEvent(new Event('pyttd/recordingProgress', {
                    frameCount: params.frameCount,
                    droppedFrames: params.droppedFrames || 0,
                    poolOverflows: params.poolOverflows || 0,
                }));
                if (params.droppedFrames > 0 && !this.droppedFrameWarningShown) {
                    this.droppedFrameWarningShown = true;
                    this.sendEvent(new Event('pyttd/error', {
                        message: `Recording is dropping frames (${params.droppedFrames} so far).`,
                        detail: 'The ring buffer is full. Try increasing checkpointInterval or reducing recording scope with --include.',
                        severity: 'warning',
                    }));
                }
                break;
            }
            case 'logpoint':
                this.sendEvent(new OutputEvent(params.message + '\n', 'console'));
                break;
            case 'conditionError':
                this.sendEvent(new OutputEvent(
                    `Breakpoint condition error at seq ${params.seq}: "${params.condition}" — ${params.error}\n`,
                    'console'
                ));
                break;
        }
    }

    protected setBreakPointsRequest(
        response: DebugProtocol.SetBreakpointsResponse,
        args: DebugProtocol.SetBreakpointsArguments
    ): void {
        const source = args.source;
        const breakpoints = args.breakpoints || [];
        const filePath = source.path || '';

        this.breakpointsByFile.set(filePath, breakpoints);

        // Build merged breakpoint list with condition, hitCondition, logMessage
        const allBreakpoints: Array<any> = [];
        for (const [file, bps] of this.breakpointsByFile) {
            for (const bp of bps) {
                const entry: any = { file, line: bp.line };
                if (bp.condition) { entry.condition = bp.condition; }
                if (bp.hitCondition) { entry.hitCondition = bp.hitCondition; }
                if (bp.logMessage) { entry.logMessage = bp.logMessage; }
                allBreakpoints.push(entry);
            }
        }

        // Forward to backend and await verification
        this.backend.sendRequest('set_breakpoints', { breakpoints: allBreakpoints })
            .then((result: any) => {
                const verified = result.verified || [];
                const responseBreakpoints = breakpoints.map((bp) => {
                    const v = verified.find((r: any) =>
                        r.file === filePath && r.line === bp.line);
                    if (v && !v.verified) {
                        return {
                            verified: false,
                            line: bp.line,
                            message: v.message || 'Breakpoint could not be verified',
                        } as DebugProtocol.Breakpoint;
                    }
                    return { verified: true, line: bp.line } as DebugProtocol.Breakpoint;
                });
                response.body = { breakpoints: responseBreakpoints };
                this.sendResponse(response);
            })
            .catch(() => {
                // Fallback: mark all verified (backend not ready or pre-recording)
                const responseBreakpoints = breakpoints.map((bp) => ({
                    verified: true, line: bp.line,
                }));
                response.body = { breakpoints: responseBreakpoints as DebugProtocol.Breakpoint[] };
                this.sendResponse(response);
            });

        // Refresh timeline breakpoint markers if in replay mode
        if (this.isReplaying) {
            this.backend.sendRequest('get_timeline_summary', {
                startSeq: this.timelineStartSeq ?? 0,
                endSeq: this.timelineEndSeq ?? this.totalFrames,
                bucketCount: 500,
            }).then((result: any) => {
                this.sendEvent(new Event('pyttd/timelineData', {
                    buckets: result.buckets,
                    totalFrames: this.totalFrames,
                    startSeq: this.timelineStartSeq ?? 0,
                    endSeq: this.timelineEndSeq ?? this.totalFrames,
                }));
            }).catch(() => {});
        }
    }

    protected setFunctionBreakPointsRequest(
        response: DebugProtocol.SetFunctionBreakpointsResponse,
        args: DebugProtocol.SetFunctionBreakpointsArguments
    ): void {
        const bps = args.breakpoints || [];
        this.functionBreakpoints = bps.map(bp => ({
            name: bp.name,
            condition: bp.condition,
            hitCondition: bp.hitCondition,
        }));

        this.backend.sendRequest('set_function_breakpoints', {
            breakpoints: this.functionBreakpoints,
        }).then((result: any) => {
            const verified = result.verified || [];
            response.body = {
                breakpoints: bps.map((bp, i) => {
                    const v = verified[i];
                    return {
                        verified: v ? v.verified !== false : true,
                        message: v?.message,
                    } as DebugProtocol.Breakpoint;
                }),
            };
            this.sendResponse(response);
        }).catch(() => {
            response.body = {
                breakpoints: bps.map(() => ({ verified: true } as DebugProtocol.Breakpoint)),
            };
            this.sendResponse(response);
        });
    }

    protected setExceptionBreakPointsRequest(
        response: DebugProtocol.SetExceptionBreakpointsResponse,
        args: DebugProtocol.SetExceptionBreakpointsArguments
    ): void {
        const filters = args.filters || [];
        this.backend
            .sendRequest('set_exception_breakpoints', { filters })
            .catch(() => {});
        this.sendResponse(response);
    }

    protected configurationDoneRequest(
        response: DebugProtocol.ConfigurationDoneResponse,
        args: DebugProtocol.ConfigurationDoneArguments
    ): void {
        this.sendEvent(new ProgressStartEvent(this.progressId, 'Recording'));

        this.backend
            .sendRequest('configuration_done')
            .then(() => {
                this.sendResponse(response);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
                this.sendEvent(new TerminatedEvent());
            });
    }

    protected threadsRequest(response: DebugProtocol.ThreadsResponse): void {
        if (this.isReplaying) {
            this.backend.sendRequest('get_threads').then(result => {
                const threads = (result.threads || []).map(
                    (t: { id: number; name: string }) => new Thread(t.id, t.name)
                );
                response.body = { threads: threads.length > 0 ? threads : [new Thread(1, 'Main Thread')] };
                this.sendResponse(response);
            }).catch(() => {
                response.body = { threads: [new Thread(1, 'Main Thread')] };
                this.sendResponse(response);
            });
        } else {
            response.body = { threads: [new Thread(1, 'Main Thread')] };
            this.sendResponse(response);
        }
    }

    protected stackTraceRequest(
        response: DebugProtocol.StackTraceResponse,
        args: DebugProtocol.StackTraceArguments
    ): void {
        if (!this.isReplaying) {
            response.body = { stackFrames: [], totalFrames: 0 };
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('get_stack_trace', { seq: this.currentSeq })
            .then((result: any) => {
                const frames: DebugProtocol.StackFrame[] = (result.frames || []).map(
                    (f: any) =>
                        new StackFrame(
                            f.seq,
                            f.name,
                            new Source(path.basename(f.file), f.file),
                            f.line
                        )
                );
                response.body = {
                    stackFrames: frames,
                    totalFrames: frames.length,
                };
                this.sendResponse(response);
            })
            .catch((err: Error) => {
                response.body = { stackFrames: [], totalFrames: 0 };
                this.sendResponse(response);
            });
    }

    protected scopesRequest(
        response: DebugProtocol.ScopesResponse,
        args: DebugProtocol.ScopesArguments
    ): void {
        const seq = args.frameId;
        const variablesReference = seq + 1;
        response.body = {
            scopes: [new Scope('Locals', variablesReference, false)],
        };
        this.sendResponse(response);
    }

    protected variablesRequest(
        response: DebugProtocol.VariablesResponse,
        args: DebugProtocol.VariablesArguments
    ): void {
        if (!this.isReplaying) {
            response.body = { variables: [] };
            this.sendResponse(response);
            return;
        }

        const backendRef = this.childRefMap.get(args.variablesReference);
        if (backendRef !== undefined) {
            // Child expansion request — get children of an expandable variable
            this.backend
                .sendRequest('get_variable_children', { variablesReference: backendRef })
                .then((result: any) => {
                    const variables: DebugProtocol.Variable[] = (result.variables || []).map(
                        (v: any) => new Variable(v.name, v.value, 0)
                    );
                    response.body = { variables };
                    this.sendResponse(response);
                })
                .catch(() => {
                    response.body = { variables: [] };
                    this.sendResponse(response);
                });
        } else {
            // Scope request — get all locals for frame
            const seq = args.variablesReference - 1;
            this.backend
                .sendRequest('get_variables', { seq })
                .then((result: any) => {
                    const variables: DebugProtocol.Variable[] = (result.variables || []).map(
                        (v: any) => {
                            let ref = 0;
                            if (v.variablesReference > 0) {
                                ref = this.nextVarRef++;
                                this.childRefMap.set(ref, v.variablesReference);
                            }
                            return new Variable(v.name, v.value, ref);
                        }
                    );
                    response.body = { variables };
                    this.sendResponse(response);
                })
                .catch(() => {
                    response.body = { variables: [] };
                    this.sendResponse(response);
                });
        }
    }

    protected evaluateRequest(
        response: DebugProtocol.EvaluateResponse,
        args: DebugProtocol.EvaluateArguments
    ): void {
        if (!this.isReplaying) {
            response.body = { result: '', variablesReference: 0 };
            this.sendResponse(response);
            return;
        }

        const evalSeq = args.frameId != null ? args.frameId : this.currentSeq;
        this.backend
            .sendRequest('evaluate', {
                seq: evalSeq,
                expression: args.expression,
                context: args.context || 'hover',
            })
            .then((result: any) => {
                response.body = {
                    result: result.result || '',
                    type: result.type,
                    variablesReference: 0,
                };
                this.sendResponse(response);
            })
            .catch(() => {
                response.body = { result: '<error>', variablesReference: 0 };
                this.sendResponse(response);
            });
    }

    protected continueRequest(
        response: DebugProtocol.ContinueResponse,
        args: DebugProtocol.ContinueArguments
    ): void {
        response.body = { allThreadsContinued: true };

        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('continue')
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason, result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected nextRequest(
        response: DebugProtocol.NextResponse,
        args: DebugProtocol.NextArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('next')
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason, result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected stepInRequest(
        response: DebugProtocol.StepInResponse,
        args: DebugProtocol.StepInArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('step_in')
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason, result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected stepOutRequest(
        response: DebugProtocol.StepOutResponse,
        args: DebugProtocol.StepOutArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('step_out')
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason, result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected pauseRequest(
        response: DebugProtocol.PauseResponse,
        args: DebugProtocol.PauseArguments
    ): void {
        if (!this.isReplaying) {
            this.backend.sendRequest('interrupt').catch(() => {});
        }
        this.sendResponse(response);
    }

    protected disconnectRequest(
        response: DebugProtocol.DisconnectResponse,
        args: DebugProtocol.DisconnectArguments
    ): void {
        this.backend
            .sendRequest('disconnect')
            .catch(() => {})
            .finally(() => {
                this.backend.close();
                this.sendResponse(response);
            });
    }

    private sendStoppedForReason(reason: string, navResult?: { seq: number; file?: string; line?: number; thread_id?: number }): void {
        // Clear variable expansion refs on navigation — stale refs from previous position
        this.childRefMap.clear();
        this.nextVarRef = 0x4000_0000;
        const threadId = navResult?.thread_id || 1;
        switch (reason) {
            case 'breakpoint':
                this.sendEvent(new StoppedEvent('breakpoint', threadId));
                break;
            case 'exception':
                this.sendEvent(new StoppedEvent('exception', threadId));
                break;
            case 'end': {
                const ev = new StoppedEvent('step', threadId) as DebugProtocol.StoppedEvent;
                ev.body.description = 'End of recording';
                ev.body.text = 'End of recording';
                this.sendEvent(ev);
                break;
            }
            case 'start': {
                const ev = new StoppedEvent('step', threadId) as DebugProtocol.StoppedEvent;
                ev.body.description = 'Beginning of recording';
                ev.body.text = 'Beginning of recording';
                this.sendEvent(ev);
                break;
            }
            case 'goto':
                this.sendEvent(new StoppedEvent('goto', threadId));
                break;
            default:
                this.sendEvent(new StoppedEvent('step', threadId));
                break;
        }
        // Send position update for timeline
        if (navResult) {
            this.sendEvent(new Event('pyttd/positionChanged', {
                seq: navResult.seq,
                file: navResult.file,
                line: navResult.line,
            }));
        }
    }

    protected stepBackRequest(
        response: DebugProtocol.StepBackResponse,
        args: DebugProtocol.StepBackArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('step_back')
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason, result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected reverseContinueRequest(
        response: DebugProtocol.ReverseContinueResponse,
        args: DebugProtocol.ReverseContinueArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('reverse_continue')
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason, result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected gotoTargetsRequest(
        response: DebugProtocol.GotoTargetsResponse,
        args: DebugProtocol.GotoTargetsArguments
    ): void {
        if (!this.isReplaying) {
            response.body = { targets: [] };
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('goto_targets', {
                filename: args.source.path || '',
                line: args.line,
            })
            .then((result: any) => {
                const targets: DebugProtocol.GotoTarget[] = (result.targets || []).map(
                    (t: any) => ({
                        id: t.seq,
                        label: `${t.function_name} @ seq ${t.seq}`,
                        line: args.line,
                    })
                );
                response.body = { targets };
                this.sendResponse(response);
            })
            .catch(() => {
                response.body = { targets: [] };
                this.sendResponse(response);
            });
    }

    protected gotoRequest(
        response: DebugProtocol.GotoResponse,
        args: DebugProtocol.GotoArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('goto_frame', { targetSeq: args.targetId })
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason || 'goto', result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected restartFrameRequest(
        response: DebugProtocol.RestartFrameResponse,
        args: DebugProtocol.RestartFrameArguments
    ): void {
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }

        this.backend
            .sendRequest('restart_frame', { frameSeq: args.frameId })
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason || 'goto', result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    }

    protected dataBreakpointInfoRequest(
        response: DebugProtocol.DataBreakpointInfoResponse,
        args: DebugProtocol.DataBreakpointInfoArguments
    ): void {
        if (!this.isReplaying) {
            response.body = { dataId: null, description: 'Not in replay mode' };
            this.sendResponse(response);
            return;
        }
        const name = args.name || '';
        response.body = {
            dataId: name,
            description: `Break when '${name}' changes value`,
            accessTypes: ['write'],
        };
        this.sendResponse(response);
    }

    protected setDataBreakpointsRequest(
        response: DebugProtocol.SetDataBreakpointsResponse,
        args: DebugProtocol.SetDataBreakpointsArguments
    ): void {
        const bps = args.breakpoints || [];
        this.dataBreakpoints = bps.map(bp => ({
            name: bp.dataId.split('.').pop() || bp.dataId,
            dataId: bp.dataId,
            accessType: bp.accessType,
        }));

        this.backend.sendRequest('set_data_breakpoints', {
            breakpoints: this.dataBreakpoints.map(bp => ({ variableName: bp.name })),
        }).then((result: any) => {
            const verified = result.verified || [];
            response.body = {
                breakpoints: bps.map((bp, i) => ({
                    verified: verified[i]?.verified !== false,
                    message: verified[i]?.message,
                } as DebugProtocol.Breakpoint)),
            };
            this.sendResponse(response);
        }).catch(() => {
            response.body = {
                breakpoints: bps.map(() => ({ verified: true } as DebugProtocol.Breakpoint)),
            };
            this.sendResponse(response);
        });
    }

    protected customRequest(command: string, response: DebugProtocol.Response, args: any): void {
        if (command === 'get_timeline_summary') {
            this.backend.sendRequest('get_timeline_summary', args)
                .then((result: any) => {
                    this.timelineStartSeq = args.startSeq;
                    this.timelineEndSeq = args.endSeq;
                    this.sendEvent(new Event('pyttd/timelineData', {
                        buckets: result.buckets,
                        totalFrames: this.totalFrames,
                        startSeq: args.startSeq,
                        endSeq: args.endSeq,
                    }));
                    this.sendResponse(response);
                })
                .catch((err: Error) => {
                    this.sendErrorResponse(response, 1, err.message);
                });
        } else if (command === 'goto_frame') {
            // Navigation handler — updates state and emits stopped event.
            // Used by CodeLens and Call History click commands.
            if (!this.isReplaying) {
                this.sendResponse(response);
                return;
            }
            this.backend.sendRequest('goto_frame', args || {})
                .then((result: any) => {
                    this.currentSeq = result.seq;
                    this.sendResponse(response);
                    this.sendStoppedForReason(result.reason || 'goto', result);
                })
                .catch((err: Error) => {
                    this.sendErrorResponse(response, 1, err.message);
                });
        } else if (['get_execution_stats', 'get_traced_files',
                     'get_call_children', 'get_variables',
                     'get_variable_history',
                     'get_checkpoint_memory'].includes(command)) {
            // Query pass-throughs — no state modification, just forward and return.
            this.backend.sendRequest(command, args || {})
                .then((result: any) => {
                    response.body = result;
                    this.sendResponse(response);
                })
                .catch((err: Error) => {
                    this.sendErrorResponse(response, 1, err.message);
                });
        } else {
            super.customRequest(command, response, args);
        }
    }
}
