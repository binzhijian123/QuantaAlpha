import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertCircle,
  BrainCircuit,
  CheckCircle2,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  WalletCards,
} from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import {
  getPaperTradingAccount,
  getPaperTradingRealtime,
  generatePaperTradingSignal,
  resetPaperTradingAccount,
  runPaperTrading,
  submitPaperTradingSignal,
} from '@/services/api';
import type { PaperRealtimeState, PaperTradingState } from '@/services/api';

type TargetRow = { id: string; symbol: string; weight: string };
type ExecutionPlan = {
  keepSymbols?: string[];
  sellSymbols?: string[];
  holdBlockedSymbols?: string[];
  buySymbols?: string[];
};

const money = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  maximumFractionDigits: 2,
});

const inputClass = 'h-10 w-full rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring';

const stageMeta: Record<string, { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  NOT_STARTED: { label: '尚未开始', variant: 'outline' },
  INVALID: { label: '观察期无效', variant: 'destructive' },
  STOPPED: { label: '已触发停止线', variant: 'destructive' },
  ENGINEERING: { label: '工程一致性检查', variant: 'warning' },
  FORWARD_OBSERVATION: { label: '前向观察中', variant: 'warning' },
  EARLY_METRIC_PASS: { label: '60 日数值门通过', variant: 'success' },
  EARLY_METRIC_FAIL: { label: '60 日数值门未过', variant: 'destructive' },
  STRONG_METRIC_PASS: { label: '120 日数值门通过', variant: 'success' },
  STRONG_METRIC_FAIL: { label: '120 日数值门未过', variant: 'destructive' },
};

const percent = (value?: number | null) => (
  value == null ? '--' : `${(value * 100).toFixed(2)}%`
);

const liveStatusMeta: Record<PaperRealtimeState['status'], { label: string; variant: 'outline' | 'warning' | 'destructive' | 'success' }> = {
  LIVE: { label: '盘中报价', variant: 'success' },
  CLOSED: { label: '收盘后报价', variant: 'outline' },
  DELAYED: { label: '报价延迟', variant: 'warning' },
  UNAVAILABLE: { label: '报价不可用', variant: 'destructive' },
};

export const PaperTradingPanel: React.FC = () => {
  const [state, setState] = useState<PaperTradingState | null>(null);
  const [liveState, setLiveState] = useState<PaperRealtimeState | null>(null);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [targets, setTargets] = useState<TargetRow[]>([
    { id: crypto.randomUUID(), symbol: '600000.SH', weight: '50' },
  ]);
  const [signalDate, setSignalDate] = useState('');
  const [signalMode, setSignalMode] = useState<'strategy' | 'manual'>('strategy');
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<'generate' | 'signal' | 'run' | 'reset' | null>(null);
  const [notice, setNotice] = useState<{ kind: 'error' | 'success'; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await getPaperTradingAccount();
      if (response.success && response.data) {
        setState(response.data);
        setSignalDate((current) => current || response.data!.marketData.latestDate);
      }
    } catch (error: any) {
      setNotice({ kind: 'error', text: error.message });
    } finally {
      setLoading(false);
    }
  }, []);

  const loadLive = useCallback(async () => {
    setLiveLoading(true);
    try {
      const response = await getPaperTradingRealtime();
      if (response.success && response.data) {
        setLiveState(response.data);
        setLiveError(null);
      }
    } catch (error: any) {
      setLiveError(error.message);
    } finally {
      setLiveLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadLive();
    const timer = window.setInterval(loadLive, 30_000);
    return () => window.clearInterval(timer);
  }, [loadLive]);

  const totalWeight = useMemo(
    () => targets.reduce((sum, target) => sum + (Number(target.weight) || 0), 0),
    [targets],
  );

  const updateTarget = (id: string, field: 'symbol' | 'weight', value: string) => {
    setTargets((rows) => rows.map((row) => (row.id === id ? { ...row, [field]: value } : row)));
  };

  const submitSignal = async () => {
    const validTargets = targets
      .filter((target) => target.symbol.trim())
      .map((target) => ({ symbol: target.symbol.trim(), targetWeight: Number(target.weight) / 100 }));
    if (!signalDate || totalWeight > 100 || validTargets.some((target) => !Number.isFinite(target.targetWeight))) {
      setNotice({ kind: 'error', text: '请检查信号日期和目标权重，合计不能超过 100%。' });
      return;
    }
    setWorking('signal');
    setNotice(null);
    try {
      const response = await submitPaperTradingSignal(signalDate, validTargets);
      setNotice({ kind: 'success', text: response.message || '目标组合已冻结，等待下一交易日。' });
      await load();
    } catch (error: any) {
      setNotice({ kind: 'error', text: error.message });
    } finally {
      setWorking(null);
    }
  };

  const generateSignal = async () => {
    if (!signalDate) {
      setNotice({ kind: 'error', text: '请选择信号日期。' });
      return;
    }
    setWorking('generate');
    setNotice(null);
    try {
      const response = await generatePaperTradingSignal(signalDate);
      if (response.data) setState(response.data);
      setNotice({ kind: 'success', text: response.message || '策略信号已生成并冻结。' });
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
      const response = await runPaperTrading();
      if (response.data) setState(response.data);
      await loadLive();
      setNotice({ kind: 'success', text: response.message || '模拟撮合完成。' });
    } catch (error: any) {
      setNotice({ kind: 'error', text: error.message });
    } finally {
      setWorking(null);
    }
  };

  const reset = async () => {
    if (!window.confirm('确认重置模拟账户？全部模拟持仓、订单和净值记录都会清空。')) return;
    setWorking('reset');
    setNotice(null);
    try {
      const response = await resetPaperTradingAccount();
      if (response.data) setState(response.data);
      await loadLive();
      setNotice({ kind: 'success', text: response.message || '模拟账户已重置。' });
    } catch (error: any) {
      setNotice({ kind: 'error', text: error.message });
    } finally {
      setWorking(null);
    }
  };

  if (loading && !state) {
    return <div className="flex min-h-[320px] items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-primary" /></div>;
  }

  if (!state) return null;

  const executionContract = state.strategy?.executionContract;
  const pendingPlan = state.pendingSignal?.source?.executionPlan as ExecutionPlan | undefined;
  const keepSymbols = pendingPlan?.keepSymbols || [];
  const sellSymbols = pendingPlan?.sellSymbols || [];
  const holdBlockedSymbols = pendingPlan?.holdBlockedSymbols || [];
  const buySymbols = pendingPlan?.buySymbols || [];
  const validation = state.paperValidation;
  const validationStage = stageMeta[validation.stage] || stageMeta.INVALID;
  const progress = Math.min(validation.observationSessions / 120, 1) * 100;
  const liveMeta = liveState ? liveStatusMeta[liveState.status] : null;
  const livePositionBySymbol = new Map((liveState?.positions || []).map((position) => [position.symbol, position]));
  const quoteTime = liveState?.quoteAsOf
    ? new Date(liveState.quoteAsOf).toLocaleString('zh-CN', { hour12: false })
    : '--';

  return (
    <div className="min-w-0 space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-2xl font-bold sm:text-3xl">
            <WalletCards className="h-7 w-7 text-primary sm:h-8 sm:w-8" />
            模拟盘验证
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            收盘后冻结换仓计划，使用下一交易日开盘行情模拟成交
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">行情截至 {state.marketData.latestDate}</Badge>
          {liveMeta && <Badge variant={liveMeta.variant}>{liveMeta.label}</Badge>}
          <Button variant="outline" size="sm" onClick={() => { load(); loadLive(); }} title="刷新账户和临时报价">
            <RefreshCw className={`h-4 w-4 ${liveLoading ? 'animate-spin' : ''}`} />
          </Button>
          <Button variant="destructive" size="sm" onClick={reset} disabled={working !== null}>
            {working === 'reset' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            重置账户
          </Button>
        </div>
      </div>

      {notice && (
        <div className={`flex items-start gap-2 rounded-md border p-3 text-sm ${notice.kind === 'error' ? 'border-red-500/40 bg-red-500/5 text-red-400' : 'border-green-500/40 bg-green-500/5 text-green-400'}`}>
          {notice.kind === 'error' ? <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> : <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />}
          <span className="break-all">{notice.text}</span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          ['正式账本净值', money.format(state.account.totalValue)],
          ['可用现金', money.format(state.account.cash)],
          ['正式持仓市值', money.format(state.account.marketValue)],
          ['累计收益', `${(state.account.totalReturn * 100).toFixed(2)}%`],
        ].map(([label, value]) => (
          <Card key={label}>
            <CardContent className="p-4">
              <div className="text-xs text-muted-foreground">{label}</div>
              <div className="mt-1 break-words text-lg font-semibold">{value}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="min-w-0">
        <CardHeader className="pb-3">
          <CardTitle className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-primary" />
              a-stock-data 临时行情
            </span>
            <span className="text-xs font-normal text-muted-foreground">每 30 秒刷新 · {quoteTime}</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="min-w-0 space-y-4">
          {liveState ? (
            <>
              <div className="grid grid-cols-2 gap-x-4 gap-y-3 lg:grid-cols-4">
                {[
                  ['临时估算净值', money.format(liveState.account.estimatedTotalValue)],
                  ['临时持仓市值', money.format(liveState.account.estimatedMarketValue)],
                  ['沪深300', liveState.benchmark?.price == null ? '--' : `${liveState.benchmark.price.toFixed(2)} (${(liveState.benchmark.changePercent || 0).toFixed(2)}%)`],
                  ['持仓报价覆盖', `${liveState.account.coveredPositions} / ${liveState.account.totalPositions}`],
                ].map(([label, value]) => (
                  <div key={label} className="min-w-0 border-b border-border/50 pb-3 lg:border-b-0 lg:pb-0">
                    <div className="text-xs text-muted-foreground">{label}</div>
                    <div className="mt-1 break-words text-base font-semibold">{value}</div>
                  </div>
                ))}
              </div>
              {(liveState.error || liveError) && (
                <div className="break-all rounded-md border border-red-500/40 bg-red-500/5 p-2 text-xs text-red-400">
                  临时报价不可用：{liveState.error || liveError}
                </div>
              )}
              <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-300">
                该层来自腾讯公共报价，只用于盘中临时估值，不写入 Qlib、信号、成交或 20/60/120 日收益证据。正式账本只在官方日线同步后更新。
              </div>
            </>
          ) : (
            <div className="py-4 text-sm text-muted-foreground">{liveError ? `临时报价加载失败：${liveError}` : '正在加载临时报价...'}</div>
          )}
        </CardContent>
      </Card>

      <Card className="min-w-0">
        <CardHeader className="pb-3">
          <CardTitle className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-primary" />
              前向验证监控
            </span>
            <Badge variant={validationStage.variant}>{validationStage.label}</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="min-w-0 space-y-4">
          <div className="grid grid-cols-2 gap-x-4 gap-y-3 lg:grid-cols-4">
            {[
              ['观察交易日', `${validation.observationSessions} / 120`],
              ['沪深300累计', percent(validation.benchmarkReturn)],
              ['税费后超额', percent(validation.afterCostExcessReturn)],
              ['最大回撤', percent(validation.maxDrawdown)],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0 border-b border-border/50 pb-3 lg:border-b-0 lg:pb-0">
                <div className="text-xs text-muted-foreground">{label}</div>
                <div className="mt-1 break-words text-base font-semibold">{value}</div>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            <div className="h-2 overflow-hidden rounded bg-secondary">
              <div className="h-full bg-primary transition-[width]" style={{ width: `${progress}%` }} />
            </div>
            <div className="flex flex-wrap gap-2">
              <Badge variant={validation.engineeringGateReached ? 'success' : validation.observationSessions >= 20 ? 'destructive' : 'outline'}>20 日工程门</Badge>
              <Badge variant={validation.earlyMetricGatePassed ? 'success' : validation.observationSessions >= 60 ? 'destructive' : 'outline'}>60 日早期数值门</Badge>
              <Badge variant={validation.strongMetricGatePassed ? 'success' : validation.observationSessions >= 120 ? 'destructive' : 'outline'}>120 日较强数值门</Badge>
            </div>
          </div>

          {validation.validationReasons.length > 0 && (
            <div className={`break-all rounded-md border p-2 text-xs ${validation.stage === 'INVALID' || validation.stage === 'STOPPED' ? 'border-red-500/40 bg-red-500/5 text-red-400' : 'border-border text-muted-foreground'}`}>
              {validation.validationReasons.join('；')}
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            数值门只计算同一冻结快照、连续交易日和完整基准下的结果，不会自动晋级 PAPER_VALIDATED。
          </p>
        </CardContent>
      </Card>

      <Card className="min-w-0">
        <CardHeader className="pb-4">
          <CardTitle className="flex flex-wrap items-center justify-between gap-2">
            <span>{signalMode === 'strategy' ? '策略换仓计划' : '手工目标组合'}</span>
            <div className="inline-flex rounded-md border border-border p-1 text-sm font-normal">
              <button
                type="button"
                aria-pressed={signalMode === 'strategy'}
                onClick={() => setSignalMode('strategy')}
                className={`rounded px-3 py-1.5 ${signalMode === 'strategy' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}
              >
                策略生成
              </button>
              <button
                type="button"
                aria-pressed={signalMode === 'manual'}
                onClick={() => setSignalMode('manual')}
                className={`rounded px-3 py-1.5 ${signalMode === 'manual' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}
              >
                手工目标
              </button>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="min-w-0 space-y-4">
          <div className="grid gap-3 sm:grid-cols-[180px_1fr] sm:items-end">
            <label className="space-y-1 text-sm">
              <span className="text-muted-foreground">信号日期</span>
              <input className={inputClass} type="date" value={signalDate} max={state.marketData.latestDate} onChange={(event) => setSignalDate(event.target.value)} />
            </label>
            <p className="text-xs text-muted-foreground sm:pb-3">
              信号只使用该日期及以前的数据，统一在下一交易日开盘模拟成交。
            </p>
          </div>

          {signalMode === 'strategy' ? (
            <div className="min-w-0 space-y-4">
              {state.strategy?.available ? (
                <div className="min-w-0 rounded-md border border-border bg-secondary/10 p-3 text-sm">
                  <div className="flex min-w-0 flex-wrap items-center gap-2 font-medium">
                    <BrainCircuit className="h-4 w-4 text-primary" />
                    冻结模型 {state.strategy.snapshotId}
                    <Badge variant="outline">{state.strategy.featureCount} 个特征</Badge>
                    <Badge variant={state.strategy.eligibleForPaper ? 'success' : 'destructive'}>
                      {state.strategy.validationStatus || 'RESEARCH_ONLY'}
                    </Badge>
                  </div>
                  {state.strategy.eligibleForPaper ? (
                    <div className="mt-2 grid gap-1 text-xs text-muted-foreground sm:grid-cols-3">
                      <span>年化收益 {((state.strategy.backtestMetrics?.annualized_return || 0) * 100).toFixed(2)}%</span>
                      <span>最大回撤 {((state.strategy.backtestMetrics?.max_drawdown || 0) * 100).toFixed(2)}%</span>
                      <span>信息比率 {(state.strategy.backtestMetrics?.information_ratio || 0).toFixed(3)}</span>
                    </div>
                  ) : (
                    <p className="mt-2 text-xs text-muted-foreground">旧快照收益指标已停用，以当前验证状态为准。</p>
                  )}
                  {!state.strategy.eligibleForPaper && (
                    <div className="mt-3 break-all rounded-md border border-red-500/40 bg-red-500/5 p-2 text-xs text-red-400">
                      {(state.strategy.validationReasons || ['策略未通过收益验证，不能生成模型信号。']).join('；')}
                    </div>
                  )}
                </div>
              ) : (
                <div className="rounded-md border border-red-500/40 bg-red-500/5 p-3 text-sm text-red-400">
                  {state.strategy?.error || '尚未生成可用的策略快照。'}
                </div>
              )}
              {executionContract && (
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">冻结持仓数量</span>
                    <input className={`${inputClass} cursor-default bg-secondary/20`} value={executionContract.topK} readOnly aria-readonly="true" />
                  </label>
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">每日最多替换</span>
                    <input className={`${inputClass} cursor-default bg-secondary/20`} value={executionContract.nDrop} readOnly aria-readonly="true" />
                  </label>
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">冻结风险仓位</span>
                    <div className="relative">
                      <input className={`${inputClass} cursor-default bg-secondary/20 pr-8`} value={(executionContract.riskDegree * 100).toFixed(0)} readOnly aria-readonly="true" />
                      <span className="pointer-events-none absolute right-3 top-2.5 text-sm text-muted-foreground">%</span>
                    </div>
                  </label>
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">最短持有交易日</span>
                    <input className={`${inputClass} cursor-default bg-secondary/20`} value={executionContract.holdThreshold} readOnly aria-readonly="true" />
                  </label>
                </div>
              )}
              <Button variant="primary" onClick={generateSignal} disabled={working !== null || !!state.pendingSignal || !state.strategy?.available || !state.strategy?.eligibleForPaper || !executionContract}>
                {working === 'generate' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <BrainCircuit className="mr-2 h-4 w-4" />}
                生成并冻结策略信号
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex justify-end text-sm text-muted-foreground">
                <span className={totalWeight > 100 ? 'text-red-400' : ''}>目标合计 {totalWeight.toFixed(1)}%</span>
              </div>
              <div className="space-y-2">
                {targets.map((target) => (
                  <div key={target.id} className="grid grid-cols-[minmax(0,1fr)_110px_40px] gap-2">
                    <input className={inputClass} value={target.symbol} placeholder="600000.SH" onChange={(event) => updateTarget(target.id, 'symbol', event.target.value)} />
                    <div className="relative">
                      <input className={`${inputClass} pr-8`} type="number" min="0" max="100" step="0.1" value={target.weight} onChange={(event) => updateTarget(target.id, 'weight', event.target.value)} />
                      <span className="pointer-events-none absolute right-3 top-2.5 text-sm text-muted-foreground">%</span>
                    </div>
                    <Button variant="ghost" className="h-10 px-0" onClick={() => setTargets((rows) => rows.filter((row) => row.id !== target.id))} title="删除目标">
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
                <Button variant="outline" onClick={() => setTargets((rows) => [...rows, { id: crypto.randomUUID(), symbol: '', weight: '' }])}>
                  <Plus className="mr-2 h-4 w-4" />添加股票
                </Button>
                <Button variant="primary" onClick={submitSignal} disabled={working !== null || !!state.pendingSignal}>
                  {working === 'signal' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                  冻结目标组合
                </Button>
              </div>
            </div>
          )}

          <div className="flex justify-end">
            <Button variant="outline" onClick={execute} disabled={working !== null || !state.pendingSignal}>
              {working === 'run' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
              执行下一交易日
            </Button>
          </div>

          {state.pendingSignal && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2 font-medium text-amber-400">
                待执行信号：{state.pendingSignal.signalDate}
                {state.pendingSignal.source?.type === 'model_snapshot' && <Badge variant="outline">模型 {state.pendingSignal.source.ref}</Badge>}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                {state.pendingSignal.source?.type === 'model_snapshot' && pendingPlan
                  ? `保留 ${keepSymbols.length} 只 · 卖出 ${sellSymbols.length} 只 · 持有期锁定 ${holdBlockedSymbols.length} 只 · 买入 ${buySymbols.length} 只`
                  : state.pendingSignal.targets.length === 0
                    ? '目标为空仓'
                    : `${state.pendingSignal.targets.slice(0, 8).map((target) => `${target.symbol} ${(target.targetWeight * 100).toFixed(1)}%`).join(' · ')}${state.pendingSignal.targets.length > 8 ? ` · 其余 ${state.pendingSignal.targets.length - 8} 只` : ''}`}
              </div>
              {state.pendingSignal.source?.type === 'model_snapshot' && pendingPlan && (
                <div className="mt-2 break-all text-xs text-muted-foreground">
                  {sellSymbols.length || holdBlockedSymbols.length || buySymbols.length
                    ? `${sellSymbols.length ? `卖出：${sellSymbols.join('、')}` : '不卖出'}；${holdBlockedSymbols.length ? `持有期锁定：${holdBlockedSymbols.join('、')}` : '无持有期锁定'}；${buySymbols.length ? `买入：${buySymbols.join('、')}` : '不买入'}`
                    : '本次不换仓，现有持仓数量保持不变。'}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>当前持仓</CardTitle></CardHeader>
        <CardContent>
          {state.positions.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground">暂无持仓</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-[900px] w-full text-sm">
                <thead className="text-left text-xs text-muted-foreground"><tr><th className="pb-3">股票</th><th>数量</th><th>可卖</th><th>持有日</th><th>成本</th><th>正式价</th><th>临时价</th><th className="text-right">临时市值</th></tr></thead>
                <tbody>{state.positions.map((position) => {
                  const livePosition = livePositionBySymbol.get(position.symbol);
                  return <tr key={position.symbol} className="border-t border-border/50"><td className="py-3 font-mono">{position.symbol}</td><td>{position.quantity}</td><td>{position.availableQuantity}</td><td>{position.holdingSessions}</td><td>{position.averageCost.toFixed(3)}</td><td>{position.lastPrice.toFixed(3)}</td><td>{livePosition?.livePrice == null ? '--' : livePosition.livePrice.toFixed(3)}</td><td className="text-right">{money.format(livePosition?.marketValue ?? position.marketValue)}</td></tr>;
                })}</tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>最近订单</CardTitle></CardHeader>
        <CardContent>
          {state.orders.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground">暂无订单记录</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-[720px] w-full text-sm">
                <thead className="text-left text-xs text-muted-foreground"><tr><th className="pb-3">日期</th><th>股票</th><th>方向</th><th>数量</th><th>状态</th><th>说明</th></tr></thead>
                <tbody>{state.orders.map((order) => <tr key={order.id} className="border-t border-border/50"><td className="py-3">{order.trade_date}</td><td className="font-mono">{order.symbol}</td><td>{order.side === 'buy' ? '买入' : '卖出'}</td><td>{order.quantity}</td><td><Badge variant={order.status === 'filled' ? 'default' : 'destructive'}>{order.status === 'filled' ? '已成交' : '未成交'}</Badge></td><td className="max-w-[260px] truncate text-muted-foreground" title={order.reason || ''}>{order.reason || '--'}</td></tr>)}</tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
};
