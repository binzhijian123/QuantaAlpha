import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  FlaskConical,
  Loader2,
  Play,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { ForwardShadowPanel } from './ForwardShadowPanel';
import {
  generateResearchShadowSignal,
  getIntradayCaptureStatus,
  getIntradayShadowAutomation,
  getIntradayShadowState,
  getResearchShadowAutomation,
  getResearchShadowRealtime,
  getResearchShadowState,
  getShadowRecoveryStatus,
  runResearchShadowPair,
} from '@/services/api';
import type {
  IntradayCaptureStatus,
  IntradayShadowAutomationStatus,
  IntradayShadowState,
  ResearchShadowArmState,
  ResearchShadowAutomationStatus,
  ResearchShadowRealtimeState,
  ResearchShadowState,
  ShadowRecoveryStatus,
} from '@/services/api';

const money = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  maximumFractionDigits: 2,
});

const percent = (value?: number | null) => (
  value == null ? '--' : `${(value * 100).toFixed(2)}%`
);

const recoveryStageLabels: Record<string, string> = {
  WAITING_CALIBRATION: '等待完整校准日',
  CALIBRATION_IN_PROGRESS: '完整日校准进行中',
  WAIT_FORWARD_RUN: '校准通过，等待未来运行验收',
  CALIBRATION_NOT_ACCEPTED: '校准未通过',
  CALIBRATION_REVIEW_REQUIRED: '校准证据待复核',
  CAPTURE_ERROR: '采集异常',
  RECOVERY_INVALID: '恢复绑定校验失败',
};

const statusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  FROZEN: { label: '模型已冻结', variant: 'outline' },
  WAITING_OFFICIAL_DAILY: { label: '等待下一交易日官方数据', variant: 'warning' },
  READY_TO_EXECUTE: { label: '可执行下一交易日', variant: 'success' },
  READY_FOR_CLOSE_SIGNAL: { label: '可生成最新收盘信号', variant: 'success' },
  MISSED_EXECUTION: { label: '已错过执行日', variant: 'destructive' },
  INVALID_PAIR: { label: '配对账本异常', variant: 'destructive' },
  RISK_STOPPED: { label: '回撤风控已停止', variant: 'destructive' },
  RETIRED: { label: '运行已退役', variant: 'destructive' },
};

const quoteStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  LIVE: { label: '盘中实时', variant: 'success' },
  CLOSED: { label: '已收盘', variant: 'outline' },
  DELAYED: { label: '报价延迟', variant: 'warning' },
  UNAVAILABLE: { label: '报价不可用', variant: 'destructive' },
};

const automationStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  NO_NEW_SESSION: { label: '等待正式日线', variant: 'outline' },
  ADVANCED: { label: '正式日线已更新', variant: 'success' },
  ADVANCED_AND_PROCESSED: { label: '日线与账本已更新', variant: 'success' },
  REVIEW_REQUIRED: { label: '需要人工复核', variant: 'destructive' },
  ERROR: { label: '自动更新异常', variant: 'destructive' },
  STARTING: { label: '守护启动中', variant: 'warning' },
  DISABLED: { label: '守护未启用', variant: 'outline' },
  RETIRED: { label: '旧运行已退役', variant: 'outline' },
};

const captureStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  STARTING: { label: '采集器启动中', variant: 'warning' },
  WAITING_MARKET: { label: '等待开市', variant: 'outline' },
  WAITING_QUOTES: { label: '等待当日报价', variant: 'warning' },
  CAPTURED: { label: '研究快照已采集', variant: 'success' },
  REUSED: { label: '研究快照已复用', variant: 'success' },
  CALENDAR_EXHAUSTED: { label: '交易日历已耗尽', variant: 'warning' },
  ERROR: { label: '研究采集异常', variant: 'destructive' },
  DISABLED: { label: '研究采集未启用', variant: 'outline' },
};

const calibrationStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  IN_PROGRESS: { label: '数据校准中', variant: 'warning' },
  PASS: { label: '数据校准通过', variant: 'success' },
  FAIL: { label: '数据校准未通过', variant: 'destructive' },
  ERROR: { label: '数据校准异常', variant: 'destructive' },
};

const temporalHealthStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  PENDING: { label: '等待连续交易', variant: 'outline' },
  IN_PROGRESS: { label: '时序采集中', variant: 'warning' },
  COMPLETE: { label: '时序完整', variant: 'success' },
  DEGRADED: { label: '时序不完整', variant: 'destructive' },
  INVALID: { label: '时序账本异常', variant: 'destructive' },
};

const intradayShadowStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  STAGED: { label: '协议已冻结，等待首日', variant: 'outline' },
  DECISION_FROZEN_AWAITING_EXECUTION: { label: '14:50 决策已冻结，等待成交', variant: 'warning' },
  AWAITING_OFFICIAL_DAILY: { label: '信号已冻结，等待正式因子', variant: 'warning' },
  ACTIVE: { label: '自动前向观察中', variant: 'success' },
  RISK_STOPPED: { label: '回撤风控已停止', variant: 'destructive' },
  MISSED_SESSION: { label: '错过冻结时槽，禁止回填', variant: 'destructive' },
  ERROR: { label: '盘中影子盘异常', variant: 'destructive' },
  STARTING: { label: '盘中影子盘启动中', variant: 'warning' },
  DISABLED: { label: '盘中影子盘未启用', variant: 'outline' },
};

const preflightStatusMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  DISABLED: { label: '自动前检未启用', variant: 'outline' },
  STARTING: { label: '自动前检等待首报', variant: 'warning' },
  READY: { label: '自动前检通过', variant: 'success' },
  NOT_READY: { label: '自动前检未通过', variant: 'destructive' },
  STALE: { label: '自动前检已过期', variant: 'destructive' },
  INVALID: { label: '自动前检报告无效', variant: 'destructive' },
};

const dateTime = (value?: string | null) => {
  if (!value) return '--';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value));
};

const plannedSymbols = (arm: ResearchShadowArmState) => {
  const desired = arm.pendingSignal?.source?.executionPlan?.desiredSymbols;
  if (Array.isArray(desired)) return desired.map(String);
  return (arm.pendingSignal?.targets || []).map((target) => target.symbol);
};

function IntradayRunCard({ intradayShadow, intradayAutomation }: {
  intradayShadow: IntradayShadowState | null;
  intradayAutomation: IntradayShadowAutomationStatus | null;
}) {
  const intradayMeta = intradayShadowStatusMeta[
    intradayAutomation?.status === 'ERROR'
      ? 'ERROR'
      : intradayAutomation?.configuredEnabled === false ? 'DISABLED'
      : intradayShadow?.status || intradayAutomation?.status || 'STARTING'
  ];
  const intradayRunVersion = intradayShadow?.runId.match(/_(v\d+)$/)?.[1]?.toUpperCase();
  const preflight = intradayAutomation?.preflight;
  const preflightMeta = preflightStatusMeta[preflight?.status || 'STARTING'] || preflightStatusMeta.INVALID;
  const preflightTitle = preflight?.failedChecks.length
    ? `失败项：${preflight.failedChecks.join('、')}`
    : undefined;
  return <>
{(intradayShadow || intradayAutomation) && (
        <Card className="min-w-0">
          <CardHeader className="pb-3">
            <CardTitle className="flex flex-wrap items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <Activity className="h-5 w-5 text-primary" />
                盘中 T+1 机制影子盘
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <Badge variant="warning">INTRADAY_SHADOW</Badge>
                {intradayRunVersion && (
                  <Badge variant="success" title={intradayShadow?.runId}>
                    {intradayAutomation?.configuredEnabled ? '运行' : '历史'} {intradayRunVersion}
                  </Badge>
                )}
                <Badge variant={intradayMeta.variant}>{intradayMeta.label}</Badge>
                {preflight && (
                  <Badge variant={preflightMeta.variant} title={preflightTitle}>
                    {preflight.status === 'READY'
                      ? `自动前检 ${preflight.passedChecks}/${preflight.totalChecks}`
                      : preflightMeta.label}
                  </Badge>
                )}
                {preflight?.checkedAt && (
                  <Badge variant="outline">检查 {dateTime(preflight.checkedAt)}</Badge>
                )}
                {intradayShadow?.audit && (
                  <Badge variant={intradayShadow.audit.valid ? 'success' : 'destructive'}>
                    审计链 {intradayShadow.audit.events} 条
                  </Badge>
                )}
                {intradayShadow?.evidenceCheckpoint && (
                  <Badge
                    variant={intradayShadow.evidenceCheckpoint.valid && intradayShadow.evidenceCheckpoint.upToDate ? 'success' : 'warning'}
                    title={intradayShadow.evidenceCheckpoint.latest?.manifestSha256 || '等待首个证据快照'}
                  >
                    证据快照 {intradayShadow.evidenceCheckpoint.count}
                  </Badge>
                )}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-start gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-300">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="space-y-1">
                <div className="font-medium">这是论文机制代理，不是论文复现，当前也没有盈利证明。</div>
                <div className="text-xs text-muted-foreground">
                  只使用未来交易日：14:50 先冻结负收益与换手率排名，随后只允许给同一目标绑定 14:55 模拟成交价，正式日线发布后结算；持仓至少跨到下一交易日，满足 T+1。
                </div>
              </div>
            </div>

            {intradayShadow?.status === 'DECISION_FROZEN_AWAITING_EXECUTION' && intradayShadow.latestDecisionIntent && (
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-2 rounded-md border border-sky-500/30 bg-sky-500/5 p-3 text-sm">
                <div className="min-w-0">
                  <div className="font-medium text-sky-300">交易目标已在成交报价出现前锁定</div>
                  <div className="mt-1 break-words text-xs text-muted-foreground">
                    交易日 {intradayShadow.latestDecisionIntent.tradeDate} · 冻结 {dateTime(intradayShadow.latestDecisionIntent.frozenAt)} · 只能等待既定 14:55 成交快照
                  </div>
                </div>
                <Badge variant="success">先决策，后成交</Badge>
              </div>
            )}

            {intradayShadow && (
              <>
                <div className="grid grid-cols-2 gap-x-4 gap-y-3 lg:grid-cols-4">
                  {[
                    ['观察交易日', `${intradayShadow.pairedEvidence.observationSessions} / 120`],
                    ['C1 税费后超额', percent(intradayShadow.pairedEvidence.c1AfterCostExcessReturn)],
                    ['C1 相对 B0', percent(intradayShadow.pairedEvidence.c1MinusB0Return)],
                    ['首个有效信号日', intradayShadow.protocol.firstEligibleSignalDate],
                  ].map(([label, value]) => (
                    <div key={label} className="min-w-0 border-b border-border/50 pb-3 lg:border-b-0 lg:pb-0">
                      <div className="text-xs text-muted-foreground">{label}</div>
                      <div className="mt-1 break-words text-base font-semibold">{value}</div>
                    </div>
                  ))}
                </div>

                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge variant="outline">决策 {intradayShadow.protocol.decisionSlot}</Badge>
                  <Badge variant="outline">模拟成交 {intradayShadow.protocol.executionSlot}</Badge>
                  {intradayShadow.causality?.decisionIntentRequired && (
                    <Badge variant="success">先决策后成交</Badge>
                  )}
                  <Badge variant="outline">Top {intradayShadow.protocol.topK}</Badge>
                  <Badge variant="outline">{intradayShadow.protocol.settlement}</Badge>
                  <Badge variant={intradayAutomation?.enabled ? 'success' : 'warning'}>
                    {intradayAutomation?.enabled ? '后端自动运行' : '自动守护未启用'}
                  </Badge>
                  <Badge variant={intradayShadow.pairedEvidence.engineeringGateReached ? 'success' : 'outline'}>20 日工程门</Badge>
                  <Badge variant={intradayShadow.pairedEvidence.earlyMetricGatePassed ? 'success' : 'outline'}>60 日早期数值门</Badge>
                  <Badge variant={intradayShadow.pairedEvidence.strongMetricGatePassed ? 'success' : 'outline'}>120 日较强数值门</Badge>
                  <Badge variant={intradayShadow.pairedEvidence.missedSessions.length ? 'destructive' : 'success'}>
                    遗漏时槽 {intradayShadow.pairedEvidence.missedSessions.length}
                  </Badge>
                  <Badge variant="outline" title={intradayShadow.declaration.sha256}>
                    声明 {intradayShadow.declaration.sha256.slice(0, 8)}
                  </Badge>
                </div>

                <div className="flex flex-wrap gap-2 border-t border-border pt-3 text-xs">
                  <Badge variant="outline">质量门 {intradayShadow.quality.requiredUniverseSize} 标的</Badge>
                  <Badge variant="outline">
                    核心字段至少 {(intradayShadow.quality.minimumCoreFieldCoverageRatio * 100).toFixed(0)}%
                  </Badge>
                  <Badge variant="outline">
                    报价滞后不超过 {intradayShadow.quality.maximumQuoteAgeSeconds} 秒
                  </Badge>
                  <Badge variant={intradayShadow.qualityExtension.requireDecisionQuotesForAllTargets ? 'success' : 'destructive'}>
                    14:50 入选逐只校验
                  </Badge>
                  <Badge variant={intradayShadow.quality.requireExecutionQuotesForAllTargets ? 'success' : 'destructive'}>
                    14:55 入选逐只校验
                  </Badge>
                  <Badge
                    variant={
                      intradayShadow.qualityExtension.requireCoreFieldAwareFallback
                      && !intradayShadow.qualityExtension.benchmarkTurnoverRequired
                        ? 'success'
                        : 'destructive'
                    }
                  >
                    核心字段逐源降级
                  </Badge>
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  {(['C1', 'B0'] as const).map((armId) => {
                    const arm = intradayShadow.arms[armId];
                    const investedValue = arm.positions.reduce(
                      (total, position) => total + position.marketValue,
                      0,
                    );
                    const investedRatio = arm.account.totalValue > 0
                      ? Math.max(0, investedValue / arm.account.totalValue)
                      : 0;
                    return (
                      <div key={armId} className="min-w-0 space-y-2">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-medium">{armId} · {arm.label}</span>
                          <span className="flex flex-wrap items-center gap-2">
                            <Badge variant="outline">持仓 {arm.positions.length}</Badge>
                            <Badge variant="outline">实际仓位 {percent(investedRatio)}</Badge>
                          </span>
                        </div>
                        <div className="grid grid-cols-2 gap-3 text-sm">
                          <div><div className="text-xs text-muted-foreground">正式模拟净值</div><div className="mt-1 font-semibold">{money.format(arm.account.totalValue)}</div></div>
                          <div><div className="text-xs text-muted-foreground">累计收益</div><div className="mt-1 font-semibold">{percent(arm.account.totalReturn)}</div></div>
                          <div><div className="text-xs text-muted-foreground">税费后超额</div><div className="mt-1 font-semibold">{percent(arm.account.afterCostExcessReturn)}</div></div>
                          <div><div className="text-xs text-muted-foreground">最大回撤</div><div className="mt-1 font-semibold">{percent(arm.maxDrawdown)}</div></div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>自动守护上次检查 {dateTime(intradayAutomation?.lastCheckedAt)}</span>
              <span>下次检查 {dateTime(intradayAutomation?.nextCheckAt)}</span>
              <span>连续错误 {intradayAutomation?.consecutiveErrors || 0}</span>
            </div>
            {intradayAutomation?.lastError && (
              <div className="break-words text-xs text-red-400">{intradayAutomation.lastError}</div>
            )}
          </CardContent>
        </Card>
      )}
  </>;
}

export const ResearchShadowPanel: React.FC = () => {
  const [state, setState] = useState<ResearchShadowState | null>(null);
  const [live, setLive] = useState<ResearchShadowRealtimeState | null>(null);
  const [automation, setAutomation] = useState<ResearchShadowAutomationStatus | null>(null);
  const [capture, setCapture] = useState<IntradayCaptureStatus | null>(null);
  const [intradayShadow, setIntradayShadow] = useState<IntradayShadowState | null>(null);
  const [intradayAutomation, setIntradayAutomation] = useState<IntradayShadowAutomationStatus | null>(null);
  const [recovery, setRecovery] = useState<ShadowRecoveryStatus | null>(null);
  const [recoveryUnavailable, setRecoveryUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [liveLoading, setLiveLoading] = useState(false);
  const [working, setWorking] = useState<'generate' | 'run' | null>(null);
  const [notice, setNotice] = useState<{ kind: 'error' | 'success'; text: string } | null>(null);

  const loadState = useCallback(async (silent = false) => {
    try {
      const response = await getResearchShadowState();
      if (response.success && response.data) setState(response.data);
    } catch (error: any) {
      if (!silent) setNotice({ kind: 'error', text: error.message });
    } finally {
      setLoading(false);
    }
  }, []);

  const loadLive = useCallback(async () => {
    setLiveLoading(true);
    try {
      const response = await getResearchShadowRealtime();
      if (response.success && response.data) setLive(response.data);
    } catch {
      // The frozen ledger remains usable when the provisional quote overlay is down.
    } finally {
      setLiveLoading(false);
    }
  }, []);

  const loadAutomation = useCallback(async () => {
    try {
      const response = await getResearchShadowAutomation();
      if (response.success && response.data) setAutomation(response.data);
    } catch {
      // Ledger controls remain fail-closed when the watcher status is unavailable.
    }
  }, []);

  const loadCapture = useCallback(async () => {
    try {
      const response = await getIntradayCaptureStatus();
      if (response.success && response.data) setCapture(response.data);
    } catch {
      // Research capture is isolated from the frozen ledger and official watcher.
    }
  }, []);

  const loadIntradayShadow = useCallback(async () => {
    try {
      const [stateResponse, automationResponse] = await Promise.all([
        getIntradayShadowState(),
        getIntradayShadowAutomation(),
      ]);
      if (stateResponse.success && stateResponse.data) setIntradayShadow(stateResponse.data);
      if (automationResponse.success && automationResponse.data) setIntradayAutomation(automationResponse.data);
    } catch {
      // The existing daily research ledger remains isolated if this new run is unavailable.
      setIntradayShadow(null);
      setIntradayAutomation(null);
    }
  }, []);

  const refresh = useCallback(async (silent = false) => {
    let current: ShadowRecoveryStatus | null = null;
    try {
      const response = await getShadowRecoveryStatus();
      if (!response.data || (!response.success && response.data.mode !== 'FORWARD_RUNTIME')) throw new Error('Recovery unavailable');
      current = response.data;
      setRecovery(current);
      setRecoveryUnavailable(false);
    } catch {
      setRecoveryUnavailable(true);
      setLoading(false);
      return;
    }
    if (current.mode !== 'FORWARD_RUNTIME') {
      void loadState(silent);
      void loadLive();
      void loadAutomation();
    } else setLoading(false);
    if (current.mode !== 'FORWARD_RUNTIME' || current.shadowEnabled) {
      void loadCapture();
      void loadIntradayShadow();
    } else {
      setCapture(null);
      setIntradayShadow(null);
      setIntradayAutomation(null);
    }
  }, [loadAutomation, loadCapture, loadIntradayShadow, loadLive, loadState]);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refresh(true);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const generate = async () => {
    setWorking('generate');
    setNotice(null);
    try {
      const response = await generateResearchShadowSignal();
      if (response.data) setState(response.data);
      setNotice({ kind: 'success', text: response.message || '配对信号已冻结。' });
      await loadLive();
    } catch (error: any) {
      setNotice({ kind: 'error', text: error.message });
    } finally {
      setWorking(null);
    }
  };

  const execute = async () => {
    setWorking('run');
    setNotice(null);
    try {
      const response = await runResearchShadowPair();
      if (response.data) setState(response.data);
      setNotice({ kind: 'success', text: response.message || '配对撮合已完成。' });
      await loadLive();
    } catch (error: any) {
      setNotice({ kind: 'error', text: error.message });
    } finally {
      setWorking(null);
    }
  };

  const armIds = useMemo(() => ['C1', 'B0'] as const, []);

  if (recovery?.mode === 'FORWARD_RUNTIME') {
    const currentEvidence = !recoveryUnavailable && intradayShadow?.runId === recovery.runId
      ? intradayShadow : null;
    return <ForwardShadowPanel runtime={recovery} state={currentEvidence}
      unavailable={recoveryUnavailable} onRefresh={() => { void refresh(true); }}>
        {currentEvidence && <IntradayRunCard intradayShadow={currentEvidence}
          intradayAutomation={intradayAutomation?.runId === recovery.runId ? intradayAutomation : null} />}
      </ForwardShadowPanel>;
  }
  if (loading && !state) {
    return <div className="flex min-h-[320px] items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-primary" /></div>;
  }
  if (!state) return <div role="status" className="p-4 text-sm text-amber-300">影子盘状态暂时无法核验，请刷新后重试。</div>;

  const meta = statusMeta[state.status] || statusMeta.INVALID_PAIR;
  const canGenerate = state.nextAction === 'GENERATE_LATEST_CLOSE_SIGNAL';
  const canExecute = state.nextAction === 'EXECUTE_NEXT_OPEN';
  const quoteState = live?.arms.C1;
  const quoteStatuses = live ? [live.arms.C1.status, live.arms.B0.status] : [];
  const quoteStatus = quoteStatuses.includes('UNAVAILABLE')
    ? 'UNAVAILABLE'
    : quoteStatuses.includes('DELAYED')
      ? 'DELAYED'
      : quoteStatuses.includes('LIVE')
        ? 'LIVE'
        : quoteStatuses.length
          ? 'CLOSED'
          : 'UNAVAILABLE';
  const quoteMeta = quoteStatusMeta[quoteStatus];
  const quoteSources = live
    ? [...new Set([...live.arms.C1.quoteSources, ...live.arms.B0.quoteSources])].sort()
    : [];
  const quoteSource = quoteSources.length === 1
    ? quoteSources[0]
    : quoteSources.length > 1
      ? 'a-stock-data/mixed'
      : 'a-stock-data/realtime-unavailable';
  const quoteTimes = live
    ? [live.arms.C1.quoteAsOf, live.arms.B0.quoteAsOf]
      .filter((value): value is string => Boolean(value))
      .sort()
    : [];
  const quoteAsOf = quoteTimes[quoteTimes.length - 1];
  const delayedSymbols = live
    ? [...new Set([
      ...live.arms.C1.delayedSymbols,
      ...live.arms.B0.delayedSymbols,
    ])].sort()
    : [];
  const officialFreshness = automation?.result?.providerUpdate?.officialDataFreshness;
  const officialOverdue = officialFreshness?.status === 'OVERDUE';
  const freshnessUnavailable = officialFreshness?.status === 'CALENDAR_UNAVAILABLE'
    || officialFreshness?.status === 'CALENDAR_EXHAUSTED';
  const automationMeta = officialOverdue
    ? { label: '正式日线已延迟', variant: 'destructive' as const }
    : freshnessUnavailable
      ? { label: '日历监控不可用', variant: 'warning' as const }
      : automationStatusMeta[automation?.status || 'STARTING'];
  const checkpoint = automation?.evidenceCheckpoint;
  const checkpointHead = checkpoint?.latest?.sha256;
  const captureMeta = captureStatusMeta[capture?.status || 'STARTING'];
  const captureCoverage = capture?.latest
    ? `${capture.latest.usableQuoteCount} / ${capture.latest.requestedCount} · ${(capture.latest.usableCoverageRatio * 100).toFixed(1)}%`
    : null;
  const calibrationMeta = calibrationStatusMeta[capture?.calibration?.status || 'IN_PROGRESS'];
  const calibrationCoverage = capture?.calibration
    ? `${capture.calibration.capturedExpectedSlotCount} / ${capture.calibration.expectedSlotCount}`
    : '0 / 242';
  const temporalHealth = capture?.temporalHealth;
  const temporalHealthMeta = temporalHealthStatusMeta[
    temporalHealth?.status || 'PENDING'
  ];
  const riskStopped = state.pairedEvidence.drawdownStopTriggered;
  const riskWarned = state.pairedEvidence.drawdownWarningTriggered;
  const intradayRunVersion = intradayShadow?.runId.match(/_(v\d+)$/)?.[1]?.toUpperCase();

  return (
    <div className="min-w-0 space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-2xl font-bold sm:text-3xl">
            <FlaskConical className="h-7 w-7 text-primary sm:h-8 sm:w-8" />
            研究影子盘
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {recoveryUnavailable ? '恢复状态暂时无法核验，请以后台检查结果为准。' : recovery?.enabled
              ? '自动影子盘恢复中；正在建立独立校准证据，历史账本保留供审计。'
              : `盘中 T+1 机制影子盘${intradayRunVersion ? `（${intradayRunVersion}）` : ''}；自动运行状态以下方开关与前检为准。`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="warning">RESEARCH_SHADOW</Badge>
          <Badge variant="outline">旧日频记录</Badge>
          <Badge variant={meta.variant}>旧记录：{meta.label}</Badge>
          <Button variant="outline" size="sm" onClick={() => { loadState(); loadLive(); loadAutomation(); loadCapture(); loadIntradayShadow(); }} title="刷新影子盘">
            <RefreshCw className={`h-4 w-4 ${liveLoading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>

      {recoveryUnavailable && (
        <div className="rounded-md border border-amber-500/30 p-4 text-sm text-amber-300" role="status">恢复状态读取失败，无法确认当前验收进度。</div>
      )}

      {recovery?.enabled && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-4 text-sm" role="status">
          <p className="font-medium text-amber-300">恢复阶段：{recoveryStageLabels[recovery.stage] || '状态待核验'} · 尚未通过自动影子盘验收</p>
          <p className="mt-1 text-muted-foreground">完整校准日：{recovery.tradeDate || '尚未声明'}；校准采集{recovery.captureEnabled ? '已启用' : '未启用'}；自动决策{recovery.shadowEnabled ? '开关异常，请检查后台' : '保持关闭'}。</p>
          {recovery.error && <p className="mt-1 break-words text-red-400">{recovery.error}</p>}
        </div>
      )}

      {notice && (
        <div className={`flex items-start gap-2 rounded-md border p-3 text-sm ${notice.kind === 'error' ? 'border-red-500/40 bg-red-500/5 text-red-400' : 'border-green-500/40 bg-green-500/5 text-green-400'}`}>
          {notice.kind === 'error' ? <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> : <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />}
          <span className="break-all">{notice.text}</span>
        </div>
      )}

      <div className="flex items-start gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-300">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
        <div className="space-y-1">
          <div className="font-medium">历史验证仍为 REJECTED，不是正式模拟盘，也不代表可以盈利。</div>
          <div className="text-xs text-muted-foreground">本盘没有手工目标、日期输入、重置或自动晋级；错过信号日或唯一下一交易日后禁止回填。</div>
        </div>
      </div>

      {(riskWarned || riskStopped) && (
        <div className={`flex items-start gap-3 rounded-md border p-4 text-sm ${riskStopped ? 'border-red-500/40 bg-red-500/5 text-red-300' : 'border-amber-500/40 bg-amber-500/5 text-amber-300'}`}>
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
          <div className="min-w-0 space-y-1">
            <div className="font-medium">
              {riskStopped
                ? '15% 回撤停止线已触发，后端已禁止新撮合和新信号。'
                : '10% 回撤预警线已触发，当前仍可观察但需要加强复核。'}
            </div>
            <div className="break-words text-xs text-muted-foreground">
              配对最差最大回撤 {percent(state.pairedEvidence.worstMaxDrawdown)}；停止后不会自动恢复，必须建立新的预声明运行。
            </div>
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-col gap-3 rounded-md border border-border bg-secondary/10 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
            <Activity className="h-4 w-4 shrink-0 text-primary" />
            盘中行情自动更新
            <Badge variant={quoteMeta.variant}>{quoteMeta.label}</Badge>
          </div>
          <div className="mt-1 break-words text-xs text-muted-foreground">
            来源 {quoteSource} · 最近报价 {dateTime(quoteAsOf)}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span>正式日线守护</span>
            <Badge variant={automationMeta.variant}>{automationMeta.label}</Badge>
            <span>上次检查 {dateTime(automation?.lastCheckedAt)}</span>
            <span>下次检查 {dateTime(automation?.nextCheckAt)}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span>盘中研究快照</span>
            <Badge variant={captureMeta.variant}>{captureMeta.label}</Badge>
            <span>最近采集 {dateTime(capture?.lastSuccessAt)}</span>
            {captureCoverage && <span>当日可用覆盖 {captureCoverage}</span>}
          </div>
          {temporalHealth && (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <span>最近交易日时序</span>
              <Badge variant={temporalHealthMeta.variant}>{temporalHealthMeta.label}</Badge>
              {temporalHealth.tradeDate && <span>{temporalHealth.tradeDate}</span>}
              {temporalHealth.integrityValid && (
                <>
                  <span>成功 {temporalHealth.capturedSlotCount || 0} / {temporalHealth.dueSlotCount || 0}</span>
                  <span>失败 {temporalHealth.errorSlotCount || 0}</span>
                  <span>缺口 {temporalHealth.missingSlotCount || 0}</span>
                  {!!temporalHealth.futureSlotCount && <span>未到期 {temporalHealth.futureSlotCount}</span>}
                  {!!temporalHealth.longestConsecutiveUnavailableSlots && (
                    <span>最长不可用 {temporalHealth.longestConsecutiveUnavailableSlots}</span>
                  )}
                </>
              )}
            </div>
          )}
          {temporalHealth?.status === 'DEGRADED' && (
            <div className="mt-1 break-words text-xs text-red-400">
              单快照覆盖不代表全天分钟完整；失败或缺口记录禁止事后补写。
            </div>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span>盘中数据校准</span>
            <Badge variant={calibrationMeta.variant}>{calibrationMeta.label}</Badge>
            <span>有效时槽 {calibrationCoverage}</span>
            {capture?.calibration?.tradeDate && (
              <span>校准日 {capture.calibration.tradeDate}</span>
            )}
          </div>
          {capture?.status === 'ERROR' && capture.lastError && (
            <div className="mt-1 break-words text-xs text-red-400">
              {capture.lastError}
            </div>
          )}
          {capture?.calibration?.status === 'ERROR' && capture.calibration.error && (
            <div className="mt-1 break-words text-xs text-red-400">
              {capture.calibration.error}
            </div>
          )}
          {officialFreshness?.expectedNextSession && (
            <div className={`mt-1 break-words text-xs ${officialOverdue ? 'text-red-400' : 'text-muted-foreground'}`}>
              下一应到 {officialFreshness.expectedNextSession} · 宽限至 {dateTime(officialFreshness.releaseDueAt)}
              {officialOverdue
                ? ` · 已延迟 ${officialFreshness.overdueSessions} 个交易日，自动账本保持不动`
                : ''}
            </div>
          )}
          {freshnessUnavailable && (
            <div className="mt-1 break-words text-xs text-amber-400">
              无法从 Provider 未来交易日历计算时效，自动账本保持不动。
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <Badge variant="outline">每 {quoteState?.pollIntervalSeconds || 30} 秒</Badge>
          <Badge variant="outline">正式日线每 {Math.max(5, Math.round((automation?.configuredPollSeconds || 900) / 60))} 分钟</Badge>
          <Badge variant="outline">研究快照每 {capture?.configuredPollSeconds || 60} 秒</Badge>
          {!!delayedSymbols.length && <Badge variant="warning">延迟代码 {delayedSymbols.length}</Badge>}
          <Badge variant="outline">临时估值</Badge>
          <Badge variant="warning">不可计入证据</Badge>
          <Badge variant="warning">日频 C1/B0 不使用盘中快照</Badge>
          <Badge variant="warning">校准日仅校准</Badge>
        </div>
      </div>

      <IntradayRunCard intradayShadow={intradayShadow} intradayAutomation={intradayAutomation} />

      <Card className="min-w-0">
        <CardHeader className="pb-3">
          <CardTitle className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-2"><Activity className="h-5 w-5 text-primary" />旧日频研究记录</span>
            <span className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">只读历史</Badge>
              <Badge variant={state.audit.valid ? 'success' : 'destructive'}>审计链 {state.audit.events} 条</Badge>
              <Badge
                variant={checkpoint?.valid && checkpoint.count > 0 ? 'success' : 'warning'}
                title={checkpointHead || '等待首个一致性快照'}
              >
                证据快照 {checkpoint?.count || 0}{checkpointHead ? ` · ${checkpointHead.slice(0, 8)}` : ''}
              </Badge>
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-300">
            此账本是旧日频研究记录，状态不会影响上方当前盘中 T+1 模拟盘（{intradayRunVersion || '读取中'}）。它保留用于审计，不会继续生成或执行交易。
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-3 lg:grid-cols-4">
            {[
              ['观察交易日', `${state.pairedEvidence.observationSessions} / 120`],
              ['C1 税费后超额', percent(state.pairedEvidence.c1AfterCostExcessReturn)],
              ['C1 相对 B0', percent(state.pairedEvidence.c1MinusB0Return)],
              ['官方行情截至', state.marketData.latestDate],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0 border-b border-border/50 pb-3 lg:border-b-0 lg:pb-0">
                <div className="text-xs text-muted-foreground">{label}</div>
                <div className="mt-1 break-words text-base font-semibold">{value}</div>
              </div>
            ))}
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge variant={state.executionContract.frozen ? 'success' : 'destructive'} title={state.executionContract.sha256}>
              执行合同 {state.executionContract.sha256.slice(0, 8)}
            </Badge>
            {state.executionImplementation && (
              <Badge
                variant={state.executionImplementation.frozen ? 'success' : 'destructive'}
                title={`${state.executionImplementation.sha256}\nPython ${state.executionImplementation.runtime.pythonVersion} · ${state.executionImplementation.files.length} 个关键文件`}
              >
                执行实现 {state.executionImplementation.sha256.slice(0, 8)}
              </Badge>
            )}
            <Badge variant="outline">开盘滑点 {state.executionContract.slippageBps}bp</Badge>
            <Badge variant="outline">
              买费 {state.executionContract.buyCostRate * 10000}bp · 卖费 {state.executionContract.sellCostRate * 10000}bp
            </Badge>
            <Badge variant="outline">最低 {state.executionContract.minimumCost} 元</Badge>
            <Badge variant="outline">
              主板/创业板 {state.executionContract.lotSize} 股
              {state.executionContract.starMinimumOrderSize
                ? ` · 科创板最低 ${state.executionContract.starMinimumOrderSize} 股`
                : ''}
              {' · '}{state.executionContract.settlement}
            </Badge>
            {state.executionContract.limitPolicy === 'causal_open_board_threshold' && (
              <Badge variant="success">因果开盘规则 · 10% / 20%</Badge>
            )}
            {state.executionContract.noLimitWindowPolicy === 'reject_first_five_or_unknown' && (
              <Badge variant="outline">上市前五日拒绝撮合</Badge>
            )}
            {!!state.executionContract.legacyDefaultsApplied.length && (
              <Badge variant="warning" title={state.executionContract.legacyDefaultsApplied.join(', ')}>兼容默认已固化</Badge>
            )}
            <Badge variant={state.pairedEvidence.engineeringGateReached ? 'success' : 'outline'}>20 日工程门</Badge>
            <Badge variant={state.pairedEvidence.earlyMetricGatePassed ? 'success' : 'outline'}>60 日早期数值门</Badge>
            <Badge variant={state.pairedEvidence.strongMetricGatePassed ? 'success' : 'outline'}>120 日较强数值门</Badge>
            <Badge variant={state.pairedEvidence.datesAligned ? 'success' : 'destructive'}>日期配对</Badge>
            <Badge variant={riskStopped ? 'destructive' : riskWarned ? 'warning' : 'outline'}>最差回撤 {percent(state.pairedEvidence.worstMaxDrawdown)}</Badge>
            <Badge variant="outline">预警线 {percent(state.protocol.drawdownWarning)}</Badge>
            <Badge variant="outline">停止线 {percent(state.protocol.drawdownStop)}</Badge>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={generate} disabled={!canGenerate || working !== null}>
              {working === 'generate' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <FlaskConical className="mr-2 h-4 w-4" />}
              冻结最新收盘配对信号
            </Button>
            <Button variant="primary" onClick={execute} disabled={!canExecute || working !== null}>
              {working === 'run' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
              执行唯一下一交易日
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {armIds.map((armId) => {
          const arm = state.arms[armId];
          const liveArm = live?.arms[armId];
          const symbols = plannedSymbols(arm);
          return (
            <Card key={armId} className="min-w-0">
              <CardHeader className="pb-3">
                <CardTitle className="flex flex-wrap items-center justify-between gap-2">
                  <span>{armId} · {arm.label}</span>
                  <Badge variant="outline">{arm.strategy.snapshotId}</Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="min-w-0 space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <div><div className="text-xs text-muted-foreground">正式账本净值</div><div className="mt-1 font-semibold">{money.format(arm.account.totalValue)}</div></div>
                  <div><div className="text-xs text-muted-foreground">临时估算净值</div><div className="mt-1 font-semibold">{liveArm ? money.format(liveArm.account.estimatedTotalValue) : '--'}</div></div>
                  <div><div className="text-xs text-muted-foreground">累计收益</div><div className="mt-1 font-semibold">{percent(arm.account.totalReturn)}</div></div>
                  <div><div className="text-xs text-muted-foreground">临时报价覆盖</div><div className="mt-1 font-semibold">{liveArm ? `${liveArm.account.coveredPositions} / ${liveArm.account.totalPositions}` : '--'}</div></div>
                </div>
                <div className="rounded-md border border-border bg-secondary/10 p-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2 font-medium">
                    {arm.pendingSignal ? `待执行：${arm.pendingSignal.signalDate}` : '暂无待执行信号'}
                    {arm.pendingSignal && <Badge variant="warning">{symbols.length} 只</Badge>}
                  </div>
                  {arm.pendingSignal && (
                    <details className="mt-3">
                      <summary className="cursor-pointer text-xs text-primary">查看冻结目标股票</summary>
                      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs sm:grid-cols-3">
                        {symbols.map((symbol) => <span key={symbol} className="truncate font-mono" title={symbol}>{symbol}</span>)}
                      </div>
                    </details>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">
                  特征 {arm.strategy.featureCount} · Top{state.protocol.topK} · 每次最多替换 {state.protocol.nDrop} · 最短持有 {state.protocol.holdThreshold} 日
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <div className="text-xs text-muted-foreground">
        旧日频记录的腾讯临时报价不入账；C1/B0 的正式成交、收益与沪深300证据只使用通过追加式历史校验的每日 Provider。旧日频运行 ID：{state.runId}
      </div>
    </div>
  );
};
