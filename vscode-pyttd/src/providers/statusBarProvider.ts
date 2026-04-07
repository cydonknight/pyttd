import * as vscode from 'vscode';

export class PyttdStatusBarProvider {
    private statusBarItem: vscode.StatusBarItem;
    private warningItem: vscode.StatusBarItem;
    private state: 'idle' | 'recording' | 'replay' | 'paused' = 'idle';
    private frameCount = 0;
    private currentSeq = 0;
    private totalFrames = 0;
    private droppedFrames = 0;
    private poolOverflows = 0;
    private recordingStartTime = 0;

    constructor() {
        this.statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Left, 50
        );
        this.statusBarItem.command = 'pyttd.focusTimeline';

        this.warningItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Left, 49
        );
        this.warningItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    }

    startRecording(): void {
        this.state = 'recording';
        this.frameCount = 0;
        this.droppedFrames = 0;
        this.poolOverflows = 0;
        this.recordingStartTime = Date.now();
        this.updateRecordingDisplay();
        this.statusBarItem.show();
        this.warningItem.hide();
    }

    updateRecording(params: { frameCount: number; droppedFrames?: number; poolOverflows?: number }): void {
        this.frameCount = params.frameCount;
        if (params.droppedFrames !== undefined) this.droppedFrames = params.droppedFrames;
        if (params.poolOverflows !== undefined) this.poolOverflows = params.poolOverflows;
        this.updateRecordingDisplay();
    }

    private updateRecordingDisplay(): void {
        const elapsed = ((Date.now() - this.recordingStartTime) / 1000).toFixed(1);
        this.statusBarItem.text = `$(record) TTD: Recording (${this.frameCount.toLocaleString()} frames, ${elapsed}s)`;
        this.statusBarItem.tooltip = `pyttd recording in progress\nFrames: ${this.frameCount.toLocaleString()}\nElapsed: ${elapsed}s`;

        if (this.droppedFrames > 0 || this.poolOverflows > 0) {
            const parts: string[] = [];
            if (this.droppedFrames > 0) parts.push(`${this.droppedFrames} dropped`);
            if (this.poolOverflows > 0) parts.push(`${this.poolOverflows} overflows`);
            this.warningItem.text = `$(warning) ${parts.join(', ')}`;
            this.warningItem.tooltip = 'Ring buffer issues detected. Try increasing checkpointInterval or reducing recording scope.';
            this.warningItem.show();
        }
    }

    enterReplay(seq: number, totalFrames: number): void {
        this.state = 'replay';
        this.currentSeq = seq;
        this.totalFrames = totalFrames;
        this.updateReplayDisplay();
        this.statusBarItem.show();
        this.warningItem.hide();
    }

    private updateReplayDisplay(): void {
        this.statusBarItem.text = `$(debug-alt) TTD: Replay (${this.currentSeq.toLocaleString()} / ${this.totalFrames.toLocaleString()})`;
        this.statusBarItem.tooltip = `pyttd replay mode\nPosition: seq ${this.currentSeq}\nTotal frames: ${this.totalFrames.toLocaleString()}\nClick to focus timeline`;
    }

    enterPaused(seq: number, totalFrames: number): void {
        this.state = 'paused';
        this.currentSeq = seq;
        this.totalFrames = totalFrames;
        this.statusBarItem.text = `$(debug-pause) TTD: Paused (${seq.toLocaleString()} / ${totalFrames.toLocaleString()})`;
        this.statusBarItem.tooltip = `pyttd paused — recording suspended\nPosition: seq ${seq}\nTotal frames: ${totalFrames.toLocaleString()}\nUse Resume Recording to continue`;
        this.statusBarItem.show();
        this.warningItem.hide();
    }

    updatePosition(seq: number): void {
        if (this.state !== 'replay' && this.state !== 'paused') return;
        this.currentSeq = seq;
        if (this.state === 'paused') {
            this.statusBarItem.text = `$(debug-pause) TTD: Paused (${seq.toLocaleString()} / ${this.totalFrames.toLocaleString()})`;
        } else {
            this.updateReplayDisplay();
        }
    }

    reset(): void {
        this.state = 'idle';
        this.statusBarItem.hide();
        this.warningItem.hide();
    }

    dispose(): void {
        this.statusBarItem.dispose();
        this.warningItem.dispose();
    }
}
