/**
 * QuantaAlpha API Service
 *
 * Centralized API client for communicating with the FastAPI backend.
 * Uses fetch (no extra dependency) with the Vite proxy (/api -> localhost:8000).
 */

import type {
  ApiResponse,
  Factor,
  Task,
  WsMessage,
} from '@/types';

// ========================== HTTP Helpers ==========================

const BASE = ''; // Vite proxy handles /api -> backend

async function request<T = any>(
  path: string,
  options: RequestInit = {},
  readableStatuses: number[] = []
): Promise<ApiResponse<T>> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers as any },
    ...options,
  });
  if (!res.ok && !readableStatuses.includes(res.status)) {
    const text = await res.text();
    throw new Error(`API Error ${res.status}: ${text}`);
  }
  return res.json();
}

// ========================== Mining API ==========================

export interface MiningStartParams {
  direction: string;
  numDirections?: number;
  maxRounds?: number;
  maxLoops?: number;
  factorsPerHypothesis?: number;
  librarySuffix?: string;
  qualityGateEnabled?: boolean;
  parallelEnabled?: boolean;
}

export async function startMining(params: MiningStartParams) {
  return request<{ taskId: string; task: Task }>('/api/v1/mining/start', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

export async function getMiningStatus(taskId: string) {
  return request<{ task: Task }>(`/api/v1/mining/${taskId}`);
}

export async function cancelMining(taskId: string) {
  return request(`/api/v1/mining/${taskId}`, { method: 'DELETE' });
}

export async function listTasks() {
  return request<{ tasks: Task[] }>('/api/v1/mining/tasks/list');
}

// ========================== Factor API ==========================

export interface FactorListParams {
  quality?: string;
  search?: string;
  limit?: number;
  offset?: number;
  library?: string;
}

export interface FactorListResponse {
  factors: Factor[];
  total: number;
  limit: number;
  offset: number;
  metadata?: any;
  libraries?: string[];
}

export async function getFactors(params: FactorListParams = {}) {
  const qs = new URLSearchParams();
  if (params.quality) qs.set('quality', params.quality);
  if (params.search) qs.set('search', params.search);
  if (params.limit) qs.set('limit', String(params.limit));
  if (params.offset) qs.set('offset', String(params.offset));
  if (params.library) qs.set('library', params.library);
  return request<FactorListResponse>(`/api/v1/factors?${qs.toString()}`);
}

export async function getFactorDetail(factorId: string) {
  return request<{ factor: any }>(`/api/v1/factors/${factorId}`);
}

export async function listFactorLibraries() {
  return request<{ libraries: string[] }>('/api/v1/factors/libraries');
}

// ========================== Factor Cache API ==========================

export interface CacheStatusResponse {
  total: number;
  h5_cached: number;
  md5_cached: number;
  need_compute: number;
  factors: Array<{
    factor_id: string;
    factor_name: string;
    status: 'h5_cached' | 'md5_cached' | 'need_compute';
  }>;
}

export interface WarmCacheResponse {
  total: number;
  synced: number;
  skipped: number;
  failed: number;
}

export async function getCacheStatus(library?: string) {
  const qs = new URLSearchParams();
  if (library) qs.set('library', library);
  return request<CacheStatusResponse>(`/api/v1/factors/cache-status?${qs.toString()}`);
}

export async function warmCache(library?: string) {
  const qs = new URLSearchParams();
  if (library) qs.set('library', library);
  return request<WarmCacheResponse>(`/api/v1/factors/warm-cache?${qs.toString()}`, {
    method: 'POST',
  });
}

// ========================== Backtest API ==========================

export interface BacktestStartParams {
  factorJson: string;
  factorSource?: string;
  configPath?: string;
}

export async function startBacktest(params: BacktestStartParams) {
  return request<{ taskId: string; task: Task }>('/api/v1/backtest/start', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

export async function getBacktestStatus(taskId: string) {
  return request<{ task: Task }>(`/api/v1/backtest/${taskId}`);
}

export async function cancelBacktest(taskId: string) {
  return request(`/api/v1/backtest/${taskId}`, { method: 'DELETE' });
}

// ========================== Paper Trading API ==========================

export interface PaperRealtimeState {
  status: 'LIVE' | 'CLOSED' | 'DELAYED' | 'UNAVAILABLE';
  source: string;
  quoteSources: string[];
  retrievedAt: string;
  quoteAsOf?: string | null;
  quoteTradeDate?: string | null;
  marketPhase: 'PRE_OPEN' | 'MORNING' | 'LUNCH' | 'AFTERNOON' | 'CLOSED' | 'NON_TRADING_DAY';
  ageSeconds?: number | null;
  delayed: boolean;
  delayedSymbols: string[];
  freshnessThresholdSeconds: number;
  provisional: true;
  persisted: false;
  evidenceEligible: false;
  pollIntervalSeconds: number;
  error?: string | null;
  officialLatestDate: string;
  reconciliationStatus: 'QUOTE_UNAVAILABLE' | 'OFFICIAL_PROVIDER_AVAILABLE' | 'AWAITING_OFFICIAL_DAILY';
  account: {
    cash: number;
    estimatedMarketValue: number;
    estimatedTotalValue: number;
    estimatedTotalReturn?: number | null;
    changeFromOfficial: number;
    coveredPositions: number;
    totalPositions: number;
    coverageComplete: boolean;
  };
  positions: Array<{
    symbol: string;
    quantity: number;
    officialLastPrice: number;
    livePrice?: number | null;
    valuationPrice: number;
    marketValue: number;
    priceSource: 'realtime' | 'official_fallback';
    quoteAsOf?: string | null;
    quoteAgeSeconds?: number | null;
    isDelayed: boolean;
    isStale: boolean;
  }>;
  benchmark?: {
    symbol: string;
    name: string;
    price?: number | null;
    previousClose?: number | null;
    change?: number | null;
    changePercent?: number | null;
    quoteAsOf?: string | null;
    quoteAgeSeconds?: number | null;
    isDelayed: boolean;
    isStale: boolean;
  } | null;
}

export interface PaperTradingState {
  account: {
    id: string;
    name: string;
    initialCash: number;
    cash: number;
    marketValue: number;
    totalValue: number;
    totalReturn: number;
    lastTradeDate?: string | null;
  };
  marketData: {
    providerPath: string;
    previousDate?: string | null;
    latestDate: string;
  };
  paperValidation: {
    benchmarkSymbol: string;
    stage: 'NOT_STARTED' | 'INVALID' | 'STOPPED' | 'ENGINEERING' | 'FORWARD_OBSERVATION' | 'EARLY_METRIC_PASS' | 'EARLY_METRIC_FAIL' | 'STRONG_METRIC_PASS' | 'STRONG_METRIC_FAIL';
    observationSessions: number;
    startDate?: string | null;
    endDate?: string | null;
    strategySnapshotId?: string | null;
    modelSessions: number;
    manualSessions: number;
    lineageValid: boolean;
    calendarComplete?: boolean | null;
    missingMarketSessions: number;
    missingBenchmarkSessions: number;
    dataComplete: boolean;
    strategyReturn?: number | null;
    benchmarkReturn?: number | null;
    afterCostExcessReturn?: number | null;
    maxDrawdown: number;
    drawdownWarningTriggered: boolean;
    drawdownStopTriggered: boolean;
    engineeringGateReached: boolean;
    earlyMetricGatePassed: boolean;
    strongMetricGatePassed: boolean;
    nextMilestone?: number | null;
    validationReasons: string[];
    automaticPromotionAllowed: false;
  };
  positions: Array<{
    symbol: string;
    quantity: number;
    availableQuantity: number;
    averageCost: number;
    lastPrice: number;
    marketValue: number;
    holdingSessions: number;
  }>;
  pendingSignal?: {
    signalId: string;
    signalDate: string;
    targets: Array<{ symbol: string; targetWeight: number }>;
    source?: Record<string, any>;
  } | null;
  latestSignal?: {
    signalId: string;
    signalDate: string;
    status: string;
    source?: Record<string, any>;
  } | null;
  strategy?: {
    available: boolean;
    eligibleForPaper?: boolean;
    validationStatus?: string;
    validationReasons?: string[];
    error?: string;
    snapshotId?: string;
    createdAt?: string;
    factorSource?: string;
    factorCount?: number;
    featureCount?: number;
    trainingSegments?: Record<string, [string, string]>;
    backtestMetrics?: Record<string, number>;
    trainingProviderPath?: string;
    signalProviderPath?: string;
    executionContract?: {
      version: number;
      type: 'topk_dropout';
      strategyClass: 'TopkDropoutStrategy';
      topK: number;
      nDrop: number;
      riskDegree: number;
      methodBuy: 'top';
      methodSell: 'bottom';
      holdThreshold: number;
      onlyTradable: boolean;
      forbidAllTradeAtLimit: boolean;
    };
  };
  orders: Array<{
    id: string;
    symbol: string;
    side: string;
    quantity: number;
    trade_date: string;
    status: string;
    reason?: string | null;
  }>;
  fills: Array<{
    id: string;
    symbol: string;
    side: string;
    quantity: number;
    trade_date: string;
    price: number;
    transaction_cost: number;
  }>;
  equityCurve: Array<{
    date: string;
    value: number;
    dailyReturn?: number | null;
    benchmarkValue?: number | null;
    benchmarkDailyReturn?: number | null;
    excessDailyReturn?: number | null;
  }>;
}

export async function getPaperTradingAccount() {
  return request<PaperTradingState>('/api/v1/paper-trading/account');
}

export async function getPaperTradingRealtime() {
  return request<PaperRealtimeState>('/api/v1/paper-trading/realtime');
}

export async function submitPaperTradingSignal(
  signalDate: string,
  targets: Array<{ symbol: string; targetWeight: number }>,
) {
  return request<{ signal: any }>('/api/v1/paper-trading/signals', {
    method: 'POST',
    body: JSON.stringify({ signalDate, targets }),
  });
}

export async function generatePaperTradingSignal(signalDate: string) {
  return request<PaperTradingState>('/api/v1/paper-trading/signals/generate', {
    method: 'POST',
    body: JSON.stringify({ signalDate }),
  });
}

export async function runPaperTrading(tradeDate?: string) {
  return request<PaperTradingState>('/api/v1/paper-trading/run', {
    method: 'POST',
    body: JSON.stringify({ tradeDate: tradeDate || null }),
  });
}

export async function resetPaperTradingAccount(initialCash = 1_000_000) {
  return request<PaperTradingState>('/api/v1/paper-trading/account/reset', {
    method: 'POST',
    body: JSON.stringify({ initialCash }),
  });
}

// ========================== Research Shadow API ==========================

export interface ResearchShadowArmState {
  arm: 'C1' | 'B0';
  label: string;
  account: PaperTradingState['account'];
  paperValidation: PaperTradingState['paperValidation'];
  positions: PaperTradingState['positions'];
  pendingSignal: PaperTradingState['pendingSignal'];
  latestSignal: PaperTradingState['latestSignal'];
  orders: PaperTradingState['orders'];
  fills: PaperTradingState['fills'];
  equityCurve: PaperTradingState['equityCurve'];
  strategy: NonNullable<PaperTradingState['strategy']> & {
    eligibleForResearchShadow: boolean;
    formalPaperEligible: false;
    usageRealm: 'RESEARCH_SHADOW';
    historicalStatus: 'REJECTED';
    automaticPromotionAllowed: false;
  };
}

export interface ResearchShadowState {
  runId: string;
  usageRealm: 'RESEARCH_SHADOW';
  status: 'FROZEN' | 'WAITING_OFFICIAL_DAILY' | 'READY_TO_EXECUTE' | 'MISSED_EXECUTION' | 'INVALID_PAIR' | 'READY_FOR_CLOSE_SIGNAL' | 'RISK_STOPPED' | 'RETIRED';
  nextAction: 'GENERATE_LATEST_CLOSE_SIGNAL' | 'WAIT_FOR_NEXT_OFFICIAL_PROVIDER' | 'EXECUTE_NEXT_OPEN' | 'START_NEW_DECLARED_RUN' | 'REVIEW_LEDGER' | 'REVIEW_RISK_STOP';
  historicalStatus: 'REJECTED';
  formalPaperEligible: false;
  automaticPromotionAllowed: false;
  lifecycle: {
    status: 'ACTIVE' | 'RETIRED';
    retiredReason?: string | null;
    replacementRunId?: string | null;
    retiredAt?: string | null;
  };
  marketData: {
    providerPath: string;
    latestDate: string;
    frozenProviderFingerprint: string;
  };
  protocol: {
    initialSignalDate: string;
    firstTradeDate: string;
    initialSignalDeadline: string;
    topK: number;
    nDrop: number;
    holdThreshold: number;
    drawdownWarning: number;
    drawdownStop: number;
  };
  executionContract: {
    version: number;
    engineModelVersion: number;
    dealPrice: 'open';
    lotSize: number;
    buyCostRate: number;
    sellCostRate: number;
    minimumCost: number;
    slippageBps: number;
    limitThreshold: number;
    growthLimitThreshold?: number;
    starMinimumOrderSize?: number;
    starOrderIncrement?: number;
    requireListingSessions?: boolean;
    transactionCostModel: 'max_minimum_or_side_rate';
    slippageModel: 'open_plus_minus_bps_clamped_to_daily_range' | 'open_plus_minus_bps_unclamped_effective_price';
    limitPolicy?: 'causal_open_board_threshold';
    noLimitWindowPolicy?: 'reject_first_five_or_unknown';
    causalKnownFields?: string[];
    forbiddenOpeningDecisionFields?: string[];
    settlement: 'T+1';
    oddLotFullExitAllowed: boolean;
    legacyDefaultsApplied: string[];
    sha256: string;
    frozen: true;
  };
  executionImplementation?: {
    version: number;
    files: Array<{ path: string; sha256: string }>;
    runtime: {
      pythonImplementation: string;
      pythonVersion: string;
      sqliteVersion: string;
      distributions: Record<string, string>;
    };
    sha256: string;
    frozen: true;
  } | null;
  pairedEvidence: {
    observationSessions: number;
    datesAligned: boolean;
    c1AfterCostExcessReturn?: number | null;
    c1MinusB0Return?: number | null;
    engineeringGateReached: boolean;
    earlyMetricGatePassed: boolean;
    strongMetricGatePassed: boolean;
    worstMaxDrawdown: number;
    drawdownWarningTriggered: boolean;
    drawdownStopTriggered: boolean;
    evidenceEligible: boolean;
  };
  arms: Record<'C1' | 'B0', ResearchShadowArmState>;
  audit: {
    valid: boolean;
    events: number;
    head?: string | null;
    lastEvent?: string | null;
    lastEventAt?: string | null;
  };
}

export interface ResearchShadowRealtimeState {
  runId: string;
  usageRealm: 'RESEARCH_SHADOW';
  formalPaperEligible: false;
  arms: Record<'C1' | 'B0', PaperRealtimeState>;
}

export interface ResearchShadowAutomationStatus {
  version: number;
  runId: string;
  enabled: boolean;
  configuredEnabled: boolean;
  status: string;
  lastCheckedAt?: string | null;
  lastSuccessAt?: string | null;
  nextCheckAt?: string | null;
  pollSeconds: number;
  configuredPollSeconds: number;
  consecutiveErrors: number;
  lastError?: string | null;
  evidenceCheckpoint?: {
    valid: boolean;
    status: 'CREATED' | 'REUSED' | 'VALID' | 'EMPTY';
    directory: string;
    count: number;
    latest?: {
      sequence: number;
      createdAt: string;
      file: string;
      sha256: string;
      evidenceSha256: string;
      sqliteFile: string;
      sqliteSha256: string;
      providerLatestDate: string;
      auditEvents: number;
      auditHead?: string | null;
    } | null;
  } | null;
  result?: {
    status: string;
    actions: string[];
    shadowStatus: string;
    nextAction: string;
    providerUpdate?: {
      status: string;
      officialRelease?: string;
      officialTargetDate?: string;
      officialDataFreshness?: {
        status: 'ON_TIME' | 'OVERDUE' | 'CALENDAR_UNAVAILABLE' | 'CALENDAR_EXHAUSTED';
        monitoringAvailable: boolean;
        activeLatestDate: string;
        expectedNextSession?: string | null;
        releaseDueAt?: string | null;
        overdueSessions: number;
        graceMinutesAfterMidnight: number;
        reason?: string | null;
      };
    };
  } | null;
}

export interface IntradayCaptureStatus {
  version: number;
  usageRealm: 'INTRADAY_RESEARCH_RAW';
  evidenceEligible: false;
  trainingEligible: false;
  enabled: boolean;
  configuredEnabled: boolean;
  configuredPollSeconds: number;
  configuredProviderPath?: string | null;
  configuredOutputRoot: string;
  configuredCalibrationEnabled?: boolean;
  configuredCalibrationDeclaration?: string;
  status: 'STARTING' | 'WAITING_MARKET' | 'WAITING_QUOTES' | 'CAPTURED' | 'REUSED' | 'CALENDAR_EXHAUSTED' | 'ERROR' | 'DISABLED';
  lastAttemptAt?: string | null;
  lastSuccessAt?: string | null;
  nextCaptureAt?: string | null;
  consecutiveErrors: number;
  lastError?: string | null;
  latest?: {
    file: string;
    contentSha256: string;
    archiveSha256: string;
    capturedAt: string;
    captureSlot: string;
    quoteAsOf?: string | null;
    quoteTradeDate?: string | null;
    providerLatestDate: string;
    requestedCount: number;
    quoteCount: number;
    currentTradeDateQuoteCount: number;
    usableQuoteCount: number;
    rawCoverageRatio: number;
    currentTradeDateCoverageRatio: number;
    usableCoverageRatio: number;
    coverageRatio: number;
    sources: string[];
  } | null;
  latestSlotRecord?: {
    file: string;
    sequence: number;
    captureSlot: string;
    status: string;
    recordSha256: string;
    previousRecordSha256?: string | null;
    committedAt: string;
  } | null;
  temporalHealth?: {
    version: 1;
    tradeDate?: string | null;
    asOf?: string | null;
    status: 'PENDING' | 'IN_PROGRESS' | 'COMPLETE' | 'DEGRADED' | 'INVALID';
    integrityValid: boolean;
    expectedSlotCount?: number;
    dueSlotCount?: number;
    futureSlotCount?: number;
    observedSlotCount?: number;
    capturedSlotCount?: number;
    errorSlotCount?: number;
    missingSlotCount?: number;
    extraSlotCount?: number;
    recordCoverageRatio?: number;
    successfulSlotCoverageRatio?: number;
    longestConsecutiveUnavailableSlots?: number;
    criticalSlots?: {
      decision: 'PENDING' | 'CAPTURED' | 'ERROR' | 'MISSING';
      execution: 'PENDING' | 'CAPTURED' | 'ERROR' | 'MISSING';
    };
    latestRecordSha256?: string | null;
    reason?: 'SLOT_CHAIN_INVALID';
  } | null;
  calibration?: {
    status: 'IN_PROGRESS' | 'PASS' | 'FAIL' | 'ERROR';
    finalized: boolean;
    decision: string;
    tradeDate?: string | null;
    file?: string | null;
    reportSha256?: string | null;
    slotCoverageRatio: number;
    medianUsableCoverageRatio: number;
    capturedExpectedSlotCount: number;
    expectedSlotCount: number;
    profitEvidenceEligible: false;
    error?: string | null;
  } | null;
}

export interface IntradayShadowArmState {
  arm: 'C1' | 'B0';
  label: string;
  account: {
    initialCash: number;
    cash: number;
    totalValue: number;
    totalReturn: number;
    benchmarkReturn: number;
    afterCostExcessReturn: number;
    lastTradeDate?: string | null;
  };
  positions: Array<{
    symbol: string;
    quantity: number;
    lastPrice: number;
    marketValue: number;
    acquiredDate: string;
  }>;
  observationSessions: number;
  maxDrawdown: number;
  drawdownWarningTriggered: boolean;
  drawdownStopTriggered: boolean;
  equityCurve: Array<Record<string, number | string | null>>;
}

export interface IntradayShadowState {
  runId: string;
  usageRealm: 'INTRADAY_SHADOW';
  status: 'STAGED' | 'DECISION_FROZEN_AWAITING_EXECUTION' | 'AWAITING_OFFICIAL_DAILY' | 'ACTIVE' | 'RISK_STOPPED' | 'MISSED_SESSION';
  formalPaperEligible: false;
  automaticPromotionAllowed: false;
  profitabilityProven: false;
  paperReplication: false;
  mechanismProxy: true;
  declaration: {
    path: string;
    sha256: string;
    declaredAt: string;
  };
  protocol: {
    firstEligibleSignalDate: string;
    decisionSlot: string;
    executionSlot: string;
    decisionFreezeAfter?: string;
    decisionFreezeDeadline?: string;
    executionBindingAfter?: string;
    executionBindingDeadline?: string;
    freezeAfter: string;
    freezeDeadline: string;
    topK: number;
    candidateFormula: string;
    baselineFormula: string;
    settlement: 'T+1';
    drawdownWarning: number;
    drawdownStop: number;
  };
  quality: {
    version: number;
    minimumCoreFieldCoverageRatio: number;
    requiredUniverseSize: number;
    maximumQuoteAgeSeconds: number;
    maximumFutureQuoteLeadSeconds: number;
    requireBenchmarkUsable: boolean;
    requireExecutionQuotesForAllTargets: boolean;
  };
  qualityExtension: {
    requireDecisionQuotesForAllTargets: boolean;
    requireCoreFieldAwareFallback: boolean;
    benchmarkTurnoverRequired: boolean;
  };
  causality?: {
    version: number;
    decisionIntentRequired: boolean;
    decisionFreezeAfter: string;
    decisionFreezeDeadline: string;
    executionBindingAfter: string;
    executionBindingDeadline: string;
    requireNoPriorPendingSession: boolean;
    requirePortfolioFingerprint: boolean;
  };
  supersedes: {
    runId: string;
    reason: string;
    supersededBeforeFirstEligibleSignalDate: boolean;
    frozenSessionsAtSupersession: number;
    observationSessionsAtSupersession: number;
    missedSessionsAtSupersession: number;
    priorRunDataPreserved: boolean;
  };
  pairedEvidence: {
    frozenSessions: number;
    decisionIntentSessions?: number;
    pendingDecisionSessions?: number;
    observationSessions: number;
    missedSessions: Array<{ trade_date: string; reason: string; recorded_at: string }>;
    c1AfterCostExcessReturn: number;
    c1MinusB0Return: number;
    engineeringGateReached: boolean;
    earlyMetricGatePassed: boolean;
    strongMetricGatePassed: boolean;
    evidenceEligible: boolean;
  };
  latestSession?: Record<string, string | null> | null;
  latestDecisionIntent?: {
    tradeDate: string;
    status: 'DECISION_FROZEN' | 'EXECUTION_BOUND';
    decisionSnapshotSha256: string;
    targetsSha256: Record<'C1' | 'B0', string>;
    frozenAt: string;
    executionBoundAt?: string | null;
  } | null;
  latestSignals: Record<'C1' | 'B0', Array<{ rank: number; symbol: string; score: number }>>;
  arms: Record<'C1' | 'B0', IntradayShadowArmState>;
  audit: {
    valid: boolean;
    events: number;
    head?: string | null;
    lastEvent?: string | null;
    lastEventAt?: string | null;
  };
  evidenceCheckpoint: {
    valid: boolean;
    status: string;
    directory: string;
    count: number;
    upToDate: boolean;
    latest?: {
      sequence: number;
      directory: string;
      createdAt?: string | null;
      manifestSha256: string;
      ledgerSha256: string;
      logicalSha256: string;
      audit: { events: number; head?: string | null };
    } | null;
  };
}

export interface IntradayShadowAutomationStatus {
  version: number;
  runId: string;
  enabled: boolean;
  configuredEnabled: boolean;
  configuredPollSeconds: number;
  configuredMaxCycleSeconds: number;
  configuredDeclaration: string;
  status: string;
  lastCheckedAt?: string | null;
  lastSuccessAt?: string | null;
  nextCheckAt?: string | null;
  pollSeconds: number;
  consecutiveErrors: number;
  lastError?: string | null;
  actions: string[];
  quality: IntradayShadowState['quality'];
  qualityExtension: IntradayShadowState['qualityExtension'];
  causality?: IntradayShadowState['causality'];
  supersedes: IntradayShadowState['supersedes'];
  preflight?: {
    configured: boolean;
    ready: boolean;
    status: 'DISABLED' | 'STARTING' | 'READY' | 'NOT_READY' | 'STALE' | 'INVALID';
    checkedAt?: string | null;
    ageSeconds?: number | null;
    maxAgeSeconds: number;
    passedChecks: number;
    totalChecks: number;
    failedChecks: string[];
  };
  state?: IntradayShadowState | null;
  evidenceCheckpoint?: IntradayShadowState['evidenceCheckpoint'] | null;
}

export async function getResearchShadowState() {
  return request<ResearchShadowState>('/api/v1/research-shadow/state');
}

export async function getResearchShadowRealtime() {
  return request<ResearchShadowRealtimeState>('/api/v1/research-shadow/realtime');
}

export async function getResearchShadowAutomation() {
  return request<ResearchShadowAutomationStatus>('/api/v1/research-shadow/automation');
}

export async function getIntradayCaptureStatus() {
  return request<IntradayCaptureStatus>('/api/v1/market-data/intraday-capture/status');
}

export async function getIntradayShadowState() {
  return request<IntradayShadowState>('/api/v1/intraday-shadow/state');
}

export async function getIntradayShadowAutomation() {
  return request<IntradayShadowAutomationStatus>('/api/v1/intraday-shadow/automation');
}

export interface ShadowRecoveryStatus {
  mode?: 'CALIBRATION_ONLY' | 'FORWARD_RUNTIME';
  configured?: boolean;
  runId?: string | null;
  declarationSha256?: string | null;
  ready?: boolean;
  maintenanceEnabled?: boolean;
  autoShadowAccepted?: boolean;
  errorCode?: string | null;
  workers?: Record<string, {
    enabled?: boolean;
    ready?: boolean;
    taskAlive?: boolean;
    status?: string;
    businessStatus?: string;
    consecutiveErrors?: number;
    lastCompletedAt?: string | null;
  }>;
  enabled: boolean;
  stage: string;
  tradeDate?: string | null;
  accepted: boolean;
  captureEnabled: boolean;
  shadowEnabled: boolean;
  error?: string | null;
}

export async function getShadowRecoveryStatus() {
  return request<ShadowRecoveryStatus>('/api/v1/shadow-recovery/status', {}, [503]);
}

export async function generateResearchShadowSignal() {
  return request<ResearchShadowState>('/api/v1/research-shadow/signals/generate', {
    method: 'POST',
  });
}

export async function runResearchShadowPair() {
  return request<ResearchShadowState>('/api/v1/research-shadow/run', {
    method: 'POST',
  });
}

// ========================== System Config API ==========================

export async function getSystemConfig() {
  return request<{ env: Record<string, string>; apiKeyConfigured: boolean; experimentYaml: string; factorLibraries: string[] }>(
    '/api/v1/system/config'
  );
}

export interface SystemEnvironment {
  pythonVersion: string;
  environmentName: string;
  checks: Array<{ name: string; passed: boolean; detail: string }>;
  scope: string;
}

export async function getSystemEnvironment() {
  return request<SystemEnvironment>('/api/v1/system/environment');
}

export async function updateSystemConfig(update: Record<string, string>) {
  return request('/api/v1/system/config', {
    method: 'PUT',
    body: JSON.stringify(update),
  });
}

// ========================== Health Check ==========================

export async function healthCheck() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    return await request<{
      status: string;
      timestamp: string;
      pid: number;
      instanceId: string;
      startedAt: string;
    }>('/api/health/ready', { signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

// ========================== WebSocket ==========================

export type WsCallback = (msg: WsMessage) => void;

export function connectMiningWs(
  taskId: string,
  onMessage: WsCallback,
  onClose?: () => void,
  onError?: (e: Event) => void
): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/mining/${taskId}`;
  const ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log(`[WS] Connected to ${taskId}`);
  };

  ws.onmessage = (event) => {
    try {
      const msg: WsMessage = JSON.parse(event.data);
      onMessage(msg);
    } catch (e) {
      console.warn('[WS] Failed to parse message:', event.data);
    }
  };

  ws.onclose = () => {
    console.log(`[WS] Disconnected from ${taskId}`);
    onClose?.();
  };

  ws.onerror = (e) => {
    console.error('[WS] Error:', e);
    onError?.(e);
  };

  // Heartbeat every 30s
  const heartbeat = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send('ping');
    } else {
      clearInterval(heartbeat);
    }
  }, 30000);

  return ws;
}
