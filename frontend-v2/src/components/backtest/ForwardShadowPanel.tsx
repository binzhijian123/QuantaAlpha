import { RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import type { IntradayShadowState, ShadowRecoveryStatus } from '@/services/api';

export function ForwardShadowPanel({ runtime, state, unavailable, onRefresh, children }: {
  runtime: ShadowRecoveryStatus;
  state: IntradayShadowState | null;
  unavailable: boolean;
  onRefresh: () => void;
  children?: ReactNode;
}) {
  const labels: Record<string, string> = { capture: '行情采集', shadow: '自动决策', maintenance: '官方数据维护' };
  const stageLabels: Record<string, string> = {
    DISABLED: '尚未配置未来运行声明', DECLARATION_INVALID: '运行绑定校验失败',
    WAITING_RUNTIME: '等待运行检查通过', FORWARD_RUNNING_UNACCEPTED: '正在运行，尚未完成验收',
  };
  return <div className="min-w-0 space-y-6">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h1 className="text-2xl font-bold">研究影子盘 · V10</h1>
        <p className="mt-1 text-sm text-muted-foreground">独立未来运行；旧历史记录保留供审计。</p></div>
      <Button variant="outline" size="sm" onClick={onRefresh} aria-label="刷新影子盘"><RefreshCw className="h-4 w-4" /></Button>
    </div>
    <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-4 text-sm" role="status">
      <p className="font-medium text-amber-300">{unavailable ? '运行状态暂时无法核验' : stageLabels[runtime.stage] || '运行状态待核验'}</p>
      <p className="mt-1 text-muted-foreground">{unavailable ? '当前配置、自动决策和运行检查均待重新核验。' : `配置${runtime.configured ? '已选择' : '未选择'}；自动决策${runtime.shadowEnabled ? '已启用' : '未启用'}；运行检查${runtime.ready ? '通过' : '未通过'}。`}</p>
      <p className="mt-1 text-muted-foreground">自动影子盘整体验收尚未通过，GitHub 发布仍待验收。运行检查通过不代表盈利有效。</p>
      {runtime.errorCode && <p className="mt-2 break-words text-red-400">{runtime.errorCode}</p>}
    </div>
    <div className="grid gap-3 md:grid-cols-3">
      {Object.entries(labels).map(([key, label]) => {
        const worker = runtime.workers?.[key];
        return <Card key={key}><CardHeader><CardTitle className="text-base">{label}</CardTitle></CardHeader>
          <CardContent className="space-y-2 break-words text-sm">
            <p>{unavailable ? '状态未知' : worker?.ready && worker.taskAlive ? '运行正常' : worker?.enabled ? '等待或异常' : '未启用'}</p>
            <p className="text-muted-foreground">检查状态：{worker?.businessStatus || worker?.status || '未知'}</p>
            <p className="text-muted-foreground">连续错误：{worker?.consecutiveErrors ?? '未知'}</p>
            <p className="text-muted-foreground">最近完成：{worker?.lastCompletedAt || '尚无记录'}</p>
          </CardContent></Card>;
      })}
    </div>
    {children}
    <Card><CardHeader><CardTitle className="text-base">真实运行证据</CardTitle></CardHeader>
      <CardContent className="space-y-2 break-words text-sm">
        <p>运行编号：{runtime.runId || '尚未声明'}</p>
        <p>首个信号日：{state?.protocol.firstEligibleSignalDate || '尚未核验'}</p>
        <p>决策冻结：{state?.latestDecisionIntent?.frozenAt || '尚无记录'}</p>
        <p>执行绑定：{state?.latestDecisionIntent?.executionBoundAt || '尚无记录'}</p>
        <p>日结观测：{state?.pairedEvidence.observationSessions ?? '未知'}；错过交易日：{state?.pairedEvidence.missedSessions.length ?? '未知'}</p>
        <p>账本审计：{state?.audit.valid === true ? '通过' : '尚未核验'}</p>
        <p className="text-muted-foreground">完整决策、执行、日结、维护及重启证据均通过后，才执行已授权的仓库覆盖。</p>
      </CardContent></Card>
  </div>;
}
