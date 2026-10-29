import React, { useRef, useEffect, useMemo, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { TimeSeriesData, RealtimeMetrics, LogEntry, DiagnosticEntry } from '@/types';
import { formatNumber, formatPercent, formatDateTime } from '@/utils';
import { TrendingUp, Activity, BarChart3, Target, AlertTriangle, Info, XCircle, ListFilter } from 'lucide-react';

interface LiveChartsProps {
  equityCurve: TimeSeriesData[];
  drawdownCurve: TimeSeriesData[];
  metrics: RealtimeMetrics | null;
  isRunning: boolean;
  logs: LogEntry[];
  diagnostics?: DiagnosticEntry[];
}

type LogFilter = 'all' | 'important' | 'errors';

export const LiveCharts: React.FC<LiveChartsProps> = ({
  metrics,
  logs,
  diagnostics = [],
}) => {
  const logContainerRef = useRef<HTMLDivElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);
  const isAutoScrollRef = useRef(true);
  const [logFilter, setLogFilter] = useState<LogFilter>('all');

  const logCounts = useMemo(() => {
    return logs.reduce(
      (acc, log) => {
        acc.all += 1;
        if (log.level !== 'info') acc.important += 1;
        if (log.level === 'error') acc.errors += 1;
        return acc;
      },
      { all: 0, important: 0, errors: 0 }
    );
  }, [logs]);

  const filteredLogs = useMemo(() => {
    if (logFilter === 'errors') {
      return logs.filter((log) => log.level === 'error');
    }
    if (logFilter === 'important') {
      return logs.filter((log) => log.level !== 'info');
    }
    return logs;
  }, [logs, logFilter]);

  const hiddenLogCount = logs.length - filteredLogs.length;

  // Handle manual scroll to toggle auto-scroll
  const handleScroll = () => {
    if (logContainerRef.current) {
      const { scrollHeight, clientHeight, scrollTop } = logContainerRef.current;
      // If user is within 50px of bottom, enable auto-scroll. Otherwise disable it.
      // Use a larger threshold (100px) to be more forgiving
      // Also ensure we handle floating point differences by checking absolute difference
      const distanceToBottom = Math.abs(scrollHeight - clientHeight - scrollTop);
      const isNearBottom = distanceToBottom < 100;
      
      // Only update if the user initiated the scroll (or if we are correcting drift)
      // This is a simple heuristic: if we are near bottom, re-enable auto-scroll
      if (isNearBottom) {
        isAutoScrollRef.current = true;
      } else {
        isAutoScrollRef.current = false;
      }
    }
  };

  useEffect(() => {
    if (isAutoScrollRef.current) {
      // Use requestAnimationFrame to ensure we scroll AFTER layout updates
      requestAnimationFrame(() => {
        if (logContainerRef.current) {
          const { scrollHeight, clientHeight } = logContainerRef.current;
          // Use scrollTo instead of scrollIntoView to avoid affecting parent containers
          logContainerRef.current.scrollTo({
            top: scrollHeight - clientHeight,
            behavior: 'smooth'
          });
        }
      });
    }
  }, [filteredLogs.length, logFilter]);

  // When mouse leaves the container, if we are near bottom, force auto-scroll to be true
  // This helps when the user was just looking at logs and moves mouse away
  const handleMouseLeave = () => {
    if (logContainerRef.current) {
      const { scrollHeight, clientHeight, scrollTop } = logContainerRef.current;
      const distanceToBottom = Math.abs(scrollHeight - clientHeight - scrollTop);
      if (distanceToBottom < 100) {
        isAutoScrollRef.current = true;
      }
    }
  };

  const getLogIcon = (level: LogEntry['level']) => {
    switch (level) {
      case 'success': return '✅';
      case 'error': return '❌';
      case 'warning': return '⚠️';
      default: return '•';
    }
  };

  const getLogColor = (level: LogEntry['level']) => {
    switch (level) {
      case 'success': return 'text-success';
      case 'error': return 'text-destructive';
      case 'warning': return 'text-warning';
      default: return 'text-muted-foreground';
    }
  };

  const getDiagnosticIcon = (severity: DiagnosticEntry['severity']) => {
    switch (severity) {
      case 'error': return XCircle;
      case 'warning': return AlertTriangle;
      default: return Info;
    }
  };

  const getDiagnosticTone = (severity: DiagnosticEntry['severity']) => {
    switch (severity) {
      case 'error':
        return {
          card: 'border-destructive/30 bg-destructive/5',
          icon: 'text-destructive',
          badge: 'destructive' as const,
        };
      case 'warning':
        return {
          card: 'border-warning/30 bg-warning/5',
          icon: 'text-warning',
          badge: 'warning' as const,
        };
      default:
        return {
          card: 'border-primary/30 bg-primary/5',
          icon: 'text-primary',
          badge: 'outline' as const,
        };
    }
  };

  const StatCard = ({ icon: Icon, label, value, trend, color }: any) => (
    <div className="glass rounded-xl p-4 card-hover h-[140px] flex flex-col justify-between">
      <div className="flex items-start justify-between mb-2">
        <div className={`p-2 rounded-lg ${color} bg-opacity-20`}>
          <Icon className={`h-5 w-5 ${color}`} />
        </div>
        {trend !== undefined && (
          <Badge variant={trend > 0 ? 'success' : 'destructive'} className="text-xs">
            {trend > 0 ? '+' : ''}{formatPercent(trend, 1)}
          </Badge>
        )}
      </div>
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-2xl font-bold">{value}</div>
    </div>
  );

  return (
    <div className="space-y-4">
      {/* Key Metrics Row */}
      {metrics && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 animate-fade-in-up">
          <StatCard
            icon={TrendingUp}
            label={metrics.factorName ? `最佳因子年化收益 (${metrics.factorName.split('_').slice(0,2).join('_')}...)` : "最佳因子年化收益"}
            value={formatPercent(metrics.annualReturn)}
            trend={metrics.annualReturn}
            color="text-success"
          />
          <StatCard
            icon={Activity}
            label="最佳因子RankIC"
            value={formatNumber(metrics.rankIc, 4)}
            color="text-primary"
          />
          <StatCard
            icon={BarChart3}
            label="最佳因子夏普比率"
            value={formatNumber(metrics.sharpeRatio, 2)}
            color="text-warning"
          />
          <StatCard
            icon={Target}
            label="最佳因子最大回撤"
            value={formatPercent(metrics.maxDrawdown)}
            trend={metrics.maxDrawdown}
            color="text-destructive"
          />
        </div>
      )}

      {/* Main Charts Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        {diagnostics.length > 0 && (
          <Card className="glass animate-fade-in-left lg:col-span-4">
            <CardHeader className="pb-3">
              <CardTitle className="text-base flex items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-warning" />
                  诊断摘要
                </span>
                <Badge variant={diagnostics.some((d) => d.severity === 'error') ? 'destructive' : 'warning'}>
                  {diagnostics.length} 条
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {diagnostics.map((diagnostic) => {
                const tone = getDiagnosticTone(diagnostic.severity);
                const Icon = getDiagnosticIcon(diagnostic.severity);
                return (
                  <div
                    key={`${diagnostic.source || 'task'}-${diagnostic.code}`}
                    className={`rounded-md border p-3 ${tone.card}`}
                  >
                    <div className="flex items-start gap-3">
                      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${tone.icon}`} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium text-sm">{diagnostic.title}</span>
                          <Badge variant={tone.badge} className="text-[10px]">
                            {diagnostic.source || 'task'}
                          </Badge>
                          {(diagnostic.count || 1) > 1 && (
                            <span className="text-[11px] text-muted-foreground">
                              x{diagnostic.count}
                            </span>
                          )}
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">{diagnostic.message}</p>
                        {diagnostic.suggestion && (
                          <p className="mt-1 text-xs">{diagnostic.suggestion}</p>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </CardContent>
          </Card>
        )}
        
        {/* Real-time Logs (Full Width) */}
        <Card className="glass card-hover animate-fade-in-left lg:col-span-4 h-[400px] flex flex-col">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex flex-wrap items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <ListFilter className="h-4 w-4 text-muted-foreground" />
                实时日志
                {hiddenLogCount > 0 && (
                  <Badge variant="outline" className="text-[10px]">
                    已隐藏 {hiddenLogCount}
                  </Badge>
                )}
              </span>
              <span className="inline-flex rounded-md border border-border bg-background/60 p-0.5">
                {([
                  ['all', '全部', logCounts.all],
                  ['important', '重点', logCounts.important],
                  ['errors', '错误', logCounts.errors],
                ] as const).map(([value, label, count]) => (
                  <Button
                    key={value}
                    type="button"
                    size="sm"
                    variant={logFilter === value ? 'secondary' : 'ghost'}
                    className="h-7 gap-1 rounded-sm px-2 text-xs"
                    onClick={() => setLogFilter(value)}
                  >
                    <span>{label}</span>
                    <span className="text-[10px] opacity-70">{count}</span>
                  </Button>
                ))}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="flex-1 min-h-0">
            <div 
              ref={logContainerRef}
              onScroll={handleScroll}
              onMouseLeave={handleMouseLeave}
              className="h-full overflow-y-auto rounded-lg bg-yellow-50 p-3 font-mono text-xs space-y-1 border border-yellow-100 scroll-smooth"
            >
              {logs.length === 0 ? (
                <div className="flex h-full items-center justify-center text-muted-foreground">
                  等待日志输出...
                </div>
              ) : filteredLogs.length === 0 ? (
                <div className="flex h-full items-center justify-center text-muted-foreground">
                  当前筛选没有匹配日志
                </div>
              ) : (
                <>
                  {filteredLogs.map((log) => (
                    <div key={log.id} className="flex gap-2 items-start animate-fade-in-up">
                      <span className="text-muted-foreground shrink-0">
                        {formatDateTime(log.timestamp).split(' ')[1]}
                      </span>
                      <span className="shrink-0">{getLogIcon(log.level)}</span>
                      <span className={getLogColor(log.level)}>{log.message}</span>
                    </div>
                  ))}
                  {/* Anchor for auto-scrolling */}
                  <div ref={logEndRef} className="h-px w-full" />
                </>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};
