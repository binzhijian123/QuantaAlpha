import React, { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Settings, Save, RotateCcw, Eye, EyeOff, Check, X, AlertCircle, Loader2, Database, Sliders, Box, Cpu, Compass, Shuffle, BookOpen, RefreshCw } from 'lucide-react';
import { getSystemConfig, updateSystemConfig, getSystemEnvironment, healthCheck, type SystemEnvironment } from '@/services/api';
import { REFERENCE_MINING_DIRECTIONS, getDirectionLabel, type MiningDirectionItem } from '@/utils/miningDirections';

interface SystemConfig {
  // LLM
  apiKey: string;
  apiUrl: string;
  modelName: string;
  reasoningModel: string;
  // Qlib
  qlibDataPath: string;
  resultsDir: string;
  // Parameters
  defaultNumDirections: number;
  defaultMaxRounds: number;
  defaultMaxLoops: number;
  defaultFactorsPerHypothesis: number;
  // Advanced
  parallelExecution: boolean;
  qualityGateEnabled: boolean;
  defaultLibrarySuffix: string;
  // Mining direction: use selected directions / random
  miningDirectionMode: 'selected' | 'random';
  selectedMiningDirectionIndices: number[];
}

const DEFAULT_CONFIG: SystemConfig = {
  apiKey: '',
  apiUrl: '',
  modelName: '',
  reasoningModel: '',
  qlibDataPath: '',
  resultsDir: '',
  defaultNumDirections: 2,
  defaultMaxRounds: 3,
  defaultMaxLoops: 2,
  defaultFactorsPerHypothesis: 1,
  parallelExecution: false,
  qualityGateEnabled: true,
  defaultLibrarySuffix: '',
  miningDirectionMode: 'selected',
  selectedMiningDirectionIndices: [0, 1, 2],
};

type SettingsTab = 'api' | 'data' | 'params' | 'directions' | 'environment';

// Migrate earlier browser preferences without retaining a previously cached key.
const readCachedConfig = (): Partial<SystemConfig> => {
  try {
    const raw = localStorage.getItem('quantaalpha_config');
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const { apiKey: _discardedKey, ...preferences } = parsed;
    if ('apiKey' in parsed) {
      try {
        localStorage.setItem('quantaalpha_config', JSON.stringify(preferences));
      } catch {
        localStorage.removeItem('quantaalpha_config');
      }
    }
    return { ...preferences, reasoningModel: preferences.reasoningModel ?? preferences.modelName ?? '' };
  } catch {
    return {};
  }
};

export const SettingsPage: React.FC = () => {
  const [config, setConfig] = useState<SystemConfig>(DEFAULT_CONFIG);
  const [activeTab, setActiveTab] = useState<SettingsTab>('api');
  const [showApiKey, setShowApiKey] = useState(false);
  const [apiKeyConfigured, setApiKeyConfigured] = useState<boolean | null>(null);
  const [environment, setEnvironment] = useState<SystemEnvironment | null>(null);
  const [checkingEnvironment, setCheckingEnvironment] = useState(false);
  const [environmentError, setEnvironmentError] = useState<string | null>(null);
  const [isSaved, setIsSaved] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [backendStatus, setBackendStatus] = useState<'checking' | 'online' | 'offline'>('checking');
  const [factorLibraries, setFactorLibraries] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Load config from backend on mount
  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    setIsLoading(true);
    setError(null);
    // Remove any key left by an older version before making network requests.
    const cached = readCachedConfig();
    try {
      await healthCheck();
      setBackendStatus('online');
    } catch {
      setBackendStatus('offline');
    }

    try {
      const resp = await getSystemConfig();
      if (!resp.success || !resp.data) throw new Error('configuration unavailable');
      const env = resp.data.env || {};
      setConfig({
        ...DEFAULT_CONFIG,
        ...cached,
        apiKey: '',
        apiUrl: env.OPENAI_BASE_URL ?? '',
        modelName: env.CHAT_MODEL ?? '',
        reasoningModel: env.REASONING_MODEL ?? env.CHAT_MODEL ?? '',
        qlibDataPath: env.QLIB_DATA_DIR ?? '',
        resultsDir: env.DATA_RESULTS_DIR ?? '',
        selectedMiningDirectionIndices: Array.isArray(cached.selectedMiningDirectionIndices)
          ? cached.selectedMiningDirectionIndices
          : DEFAULT_CONFIG.selectedMiningDirectionIndices,
      });
      setApiKeyConfigured(resp.data.apiKeyConfigured === true);
      setFactorLibraries(resp.data.factorLibraries || []);
    } catch {
      setConfig({
        ...DEFAULT_CONFIG,
        ...cached,
        apiKey: '',
        selectedMiningDirectionIndices: Array.isArray(cached.selectedMiningDirectionIndices)
          ? cached.selectedMiningDirectionIndices
          : DEFAULT_CONFIG.selectedMiningDirectionIndices,
      });
      setError('无法从后端加载配置。当前仅显示浏览器偏好，请检查后端连接后重试。');
    } finally {
      setIsLoading(false);
    }
  };

  const refreshEnvironment = async () => {
    setCheckingEnvironment(true);
    setEnvironmentError(null);
    try {
      const resp = await getSystemEnvironment();
      if (!resp.success || !resp.data) throw new Error('environment unavailable');
      setEnvironment(resp.data);
    } catch {
      setEnvironmentError('环境检查暂时不可用，请确认后端已启动后重试。');
    } finally {
      setCheckingEnvironment(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'environment') refreshEnvironment();
  }, [activeTab]);

  const handleSave = async () => {
    setIsSaving(true);
    setIsSaved(false);
    setError(null);
    try {
      const update: Record<string, string> = {
        OPENAI_BASE_URL: config.apiUrl.trim(),
        CHAT_MODEL: config.modelName.trim(),
        REASONING_MODEL: config.reasoningModel.trim(),
        QLIB_DATA_DIR: config.qlibDataPath.trim(),
        QLIB_PROVIDER_URI: config.qlibDataPath.trim(),
        DATA_RESULTS_DIR: config.resultsDir.trim(),
      };
      if (config.apiKey.trim()) update.OPENAI_API_KEY = config.apiKey.trim();
      const resp = await updateSystemConfig(update);
      if (!resp.success) throw new Error('configuration rejected');

      const { apiKey: _key, ...preferences } = config;
      let preferencesSaved = true;
      try {
        localStorage.setItem('quantaalpha_config', JSON.stringify(preferences));
      } catch {
        preferencesSaved = false;
        setError('后端配置已保存，但浏览器偏好写入失败。请检查浏览器存储权限后重试。');
      }
      if (config.apiKey.trim()) setApiKeyConfigured(true);
      setConfig((current) => ({ ...current, apiKey: '' }));
      setShowApiKey(false);
      setIsSaved(preferencesSaved);
      setIsDirty(!preferencesSaved);
      setEnvironment(null);
      setTimeout(() => setIsSaved(false), 3000);
    } catch {
      // Never log request bodies or service errors that could contain credentials.
      setError('配置未保存成功。请检查后端连接及填写内容后重试；更改仍保留在表单中。');
      setIsDirty(true);
    } finally {
      setIsSaving(false);
    }
  };

  const handleReset = () => {
    if (confirm('确定要重置为默认配置吗？')) {
      setConfig(DEFAULT_CONFIG);
      setIsDirty(true);
    }
  };

  const updateConfigField = (key: keyof SystemConfig, value: any) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
    setIsDirty(true);
    setIsSaved(false);
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
        <span className="ml-3 text-muted-foreground">加载配置中...</span>
      </div>
    );
  }

  const TabButton = ({ id, label, icon: Icon }: { id: SettingsTab; label: string; icon: any }) => (
    <button
      onClick={() => setActiveTab(id)}
      disabled={isSaving}
      aria-current={activeTab === id ? 'page' : undefined}
      className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-all ${
        activeTab === id
          ? 'bg-primary text-primary-foreground shadow-lg scale-105'
          : 'text-muted-foreground hover:bg-secondary/50 hover:text-foreground'
      }`}
    >
      <Icon className="h-4 w-4" />
      <span className="font-medium">{label}</span>
    </button>
  );

  return (
    <div className="space-y-6 animate-fade-in-up">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-3">
            <Settings className="h-8 w-8 text-primary" />
            系统配置
          </h1>
          <p className="text-muted-foreground mt-1">
            管理 API 连接、数据源及实验参数
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <Button variant="outline" onClick={handleReset} disabled={isSaving}>
            <RotateCcw className="h-4 w-4 mr-2" />
            重置
          </Button>
          <Button variant="primary" onClick={handleSave} disabled={!isDirty || isSaving}>
            {isSaving ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Save className="h-4 w-4 mr-2" />
            )}
            保存配置
          </Button>
        </div>
      </div>

      {/* Status Banners */}
      {isSaved && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-success/10 border-success/50 animate-fade-in-down">
          <Check className="h-5 w-5 text-success" />
          <span className="text-success">配置已保存。API、模型及路径用于后续新任务；任务偏好已保存到当前浏览器。</span>
        </div>
      )}
      {isDirty && !isSaved && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-warning/10 border-warning/50 animate-fade-in-down">
          <X className="h-5 w-5 text-warning" />
          <span className="text-warning">有未保存的更改</span>
        </div>
      )}
      {error && (
        <div role="alert" className="glass rounded-lg p-4 flex items-center gap-3 bg-warning/10 border-warning/50">
          <AlertCircle className="h-5 w-5 text-warning flex-shrink-0" />
          <span className="text-sm text-warning">{error}</span>
        </div>
      )}

      {/* Tabs Navigation */}
      <div className="flex gap-2 p-1 bg-secondary/20 rounded-xl w-fit flex-wrap">
        <TabButton id="api" label="配置 API" icon={Cpu} />
        <TabButton id="data" label="数据路径" icon={Database} />
        <TabButton id="params" label="默认参数" icon={Sliders} />
        <TabButton id="directions" label="挖掘方向" icon={Compass} />
        <TabButton id="environment" label="环境与说明" icon={BookOpen} />
      </div>

      {/* Tab Content */}
      <fieldset disabled={isSaving} className="min-w-0 grid grid-cols-1 gap-6">
        
        {/* API Configuration Tab */}
        {activeTab === 'api' && (
          <Card className="min-w-0 glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                🤖 LLM 模型配置
                <Badge variant="default">核心</Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="min-w-0 space-y-6">
              <div>
                <label htmlFor="settings-api-key" className="block text-sm font-medium mb-2">
                  API Key {apiKeyConfigured === false && <span className="text-destructive">*</span>}
                </label>
                <div className="flex gap-2">
                  <input
                    id="settings-api-key"
                    autoComplete="off"
                    spellCheck={false}
                    type={showApiKey ? 'text' : 'password'}
                    value={config.apiKey}
                    onChange={(e) => updateConfigField('apiKey', e.target.value)}
                    placeholder={apiKeyConfigured ? '密钥已配置；留空保留原值' : '输入你自己的 API Key'}
                    className="min-w-0 flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <Button
                    variant="outline"
                    onClick={() => setShowApiKey(!showApiKey)}
                    aria-label={showApiKey ? '隐藏本次输入的密钥' : '显示本次输入的密钥'}
                    aria-pressed={showApiKey}
                    className="px-3"
                  >
                    {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  {apiKeyConfigured === null ? '密钥配置状态未知，请确认后端连接。' : apiKeyConfigured ? '后端已配置密钥。' : '尚未配置密钥。'} 留空不会清除原密钥；密钥不会回显或保存到浏览器缓存。
                </p>
              </div>

              <div>
                <label htmlFor="settings-api-url" className="block text-sm font-medium mb-2">API Base URL</label>
                <input
                  type="text"
                  id="settings-api-url"
                  value={config.apiUrl}
                  onChange={(e) => updateConfigField('apiUrl', e.target.value)}
                  placeholder="https://你的服务地址/v1"
                  className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  填写服务商提供的完整兼容接口地址，与密钥对应。
                </p>
              </div>

              <div>
                <label htmlFor="settings-model-name" className="block text-sm font-medium mb-2">聊天模型 ID</label>
                <input
                  id="settings-model-name"
                  type="text"
                  value={config.modelName}
                  onChange={(e) => updateConfigField('modelName', e.target.value)}
                  placeholder="填写服务商控制台中的模型 ID"
                  className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                />
                <p className="text-xs text-muted-foreground mt-1">填写服务商提供的聊天模型 ID，按原样保存。</p>
              </div>

              <div>
                <label htmlFor="settings-reasoning-model" className="block text-sm font-medium mb-2">推理模型 ID</label>
                <input
                  id="settings-reasoning-model"
                  type="text"
                  value={config.reasoningModel}
                  onChange={(e) => updateConfigField('reasoningModel', e.target.value)}
                  placeholder="填写服务商控制台中的推理模型 ID"
                  className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                />
                <p className="text-xs text-muted-foreground mt-1">聊天模型与推理模型分别保存，可以填写相同的 ID。</p>
              </div>

              <p className="text-sm text-muted-foreground">后端已连接只表示本地服务可访问，不代表 API 密钥、模型权限或余额已验证。</p>

              {/* Connection Status */}
              <div className="pt-4 border-t border-border/50">
                <div className="flex items-center gap-3">
                  <div
                    className={`h-3 w-3 rounded-full ${
                      backendStatus === 'online'
                        ? 'bg-success animate-pulse'
                        : backendStatus === 'offline'
                        ? 'bg-destructive'
                        : 'bg-warning animate-pulse'
                    }`}
                  />
                  <span className="text-sm">
                    后端连接状态：
                    {backendStatus === 'online' ? <span className="text-success font-medium">已连接</span> : 
                     backendStatus === 'offline' ? <span className="text-destructive font-medium">未连接</span> : 
                     '检测中...'}
                  </span>
                </div>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Data Path Configuration Tab */}
        {activeTab === 'data' && (
          <Card className="min-w-0 glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                📊 数据存储路径
              </CardTitle>
            </CardHeader>
            <CardContent className="min-w-0 space-y-6">
              <p className="text-sm text-muted-foreground leading-relaxed">
                填写运行后端的电脑上的目录。数据需由使用者自行准备，仓库不包含行情数据或其他人的本机配置。
                保存后可在“环境与说明”检查已保存路径。
              </p>

              <div>
                <label htmlFor="settings-qlib-path" className="block text-sm font-medium mb-2">
                  Qlib 数据目录 <span className="text-destructive">*</span>
                </label>
                <div className="flex items-center gap-2">
                  <Database className="h-4 w-4 text-muted-foreground" />
                  <input
                    type="text"
                    id="settings-qlib-path"
                    value={config.qlibDataPath}
                    onChange={(e) => updateConfigField('qlibDataPath', e.target.value)}
                    placeholder="/path/to/qlib/cn_data"
                    className="min-w-0 flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                </div>
                <p className="text-xs text-muted-foreground mt-1 ml-6">
                  需包含 calendars/, features/, instruments/ 等 Qlib 标准数据子目录
                </p>
              </div>

              <div>
                <label htmlFor="settings-results-path" className="block text-sm font-medium mb-2">
                  实验结果输出目录
                </label>
                <div className="flex items-center gap-2">
                  <Box className="h-4 w-4 text-muted-foreground" />
                  <input
                    type="text"
                    id="settings-results-path"
                    value={config.resultsDir}
                    onChange={(e) => updateConfigField('resultsDir', e.target.value)}
                    placeholder="/path/to/results"
                    className="min-w-0 flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                </div>
                <p className="text-xs text-muted-foreground mt-1 ml-6">
                  用于存放挖掘出的因子、回测报告及日志文件
                </p>
              </div>

              {factorLibraries.length > 0 && (
                <div className="min-w-0 bg-secondary/20 rounded-lg p-4 mt-4">
                  <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
                    <Check className="h-4 w-4 text-success" />
                    已识别的因子库
                  </h4>
                  <div className="min-w-0 flex flex-wrap gap-2">
                    {factorLibraries.map((lib, idx) => (
                      <Badge
                        key={idx}
                        variant="outline"
                        className="min-w-0 max-w-full break-all whitespace-normal bg-background/50"
                      >
                        {lib}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {/* Default Parameters Tab */}
        {activeTab === 'params' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                ⚙️ 实验默认参数
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label htmlFor="settings-defaultNumDirections" className="block text-sm font-medium mb-2">初始探索分支数</label>
                  <input
                    type="number"
                    id="settings-defaultNumDirections"
                    value={config.defaultNumDirections}
                    onChange={(e) => updateConfigField('defaultNumDirections', parseInt(e.target.value))}
                    min={1}
                    max={10}
                    className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    写入 planning.num_directions；1 表示直接使用当前方向
                  </p>
                </div>

                <div>
                  <label htmlFor="settings-defaultMaxRounds" className="block text-sm font-medium mb-2">进化总轮次</label>
                  <input
                    type="number"
                    id="settings-defaultMaxRounds"
                    value={config.defaultMaxRounds}
                    onChange={(e) => updateConfigField('defaultMaxRounds', parseInt(e.target.value))}
                    min={1}
                    max={20}
                    className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    写入 evolution.max_rounds
                  </p>
                </div>

                <div>
                  <label htmlFor="settings-defaultMaxLoops" className="block text-sm font-medium mb-2">每任务循环数</label>
                  <input
                    type="number"
                    id="settings-defaultMaxLoops"
                    value={config.defaultMaxLoops}
                    onChange={(e) => updateConfigField('defaultMaxLoops', parseInt(e.target.value))}
                    min={1}
                    max={10}
                    className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    写入 execution.max_loops；1 个循环为 5 个工作流步骤
                  </p>
                </div>

                <div>
                  <label htmlFor="settings-defaultFactorsPerHypothesis" className="block text-sm font-medium mb-2">每个假设因子数</label>
                  <input
                    type="number"
                    id="settings-defaultFactorsPerHypothesis"
                    value={config.defaultFactorsPerHypothesis}
                    onChange={(e) => updateConfigField('defaultFactorsPerHypothesis', parseInt(e.target.value))}
                    min={1}
                    max={5}
                    className="w-full min-w-0 rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    写入 factor.factors_per_hypothesis
                  </p>
                </div>

                <div className="md:col-span-2">
                  <label htmlFor="settings-defaultLibrarySuffix" className="block text-sm font-medium mb-2">默认因子库名称后缀</label>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-muted-foreground font-mono break-all">all_factors_library_</span>
                    <input
                      type="text"
                      id="settings-defaultLibrarySuffix"
                      value={config.defaultLibrarySuffix}
                      onChange={(e) => {
                        const val = e.target.value.replace(/[^a-zA-Z0-9_\-]/g, '');
                        updateConfigField('defaultLibrarySuffix', val);
                      }}
                      placeholder="例如 momentum_v1 (留空则无后缀)"
                      className="min-w-0 flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                    />
                    <span className="text-sm text-muted-foreground font-mono">.json</span>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    生成的因子将保存到此文件。支持字母、数字、下划线。
                  </p>
                </div>
              </div>

              <div className="pt-4 border-t border-border/50 space-y-4">
                <h4 className="text-sm font-medium">高级控制</h4>
                
                <label className="flex items-center gap-3 cursor-pointer group p-3 rounded-lg border border-border/50 hover:bg-secondary/20 transition-all">
                  <input
                    type="checkbox"
                    checked={config.parallelExecution}
                    onChange={(e) => updateConfigField('parallelExecution', e.target.checked)}
                    className="h-5 w-5 rounded border-input text-primary focus:ring-primary"
                  />
                  <div className="flex-1">
                    <div className="font-medium group-hover:text-primary transition-colors">
                      启用并行执行
                    </div>
                    <div className="text-xs text-muted-foreground">
                      允许多个挖掘方向同时运行，显著加快实验速度，但会增加系统负载
                    </div>
                  </div>
                </label>

                <label className="flex items-center gap-3 cursor-pointer group p-3 rounded-lg border border-border/50 hover:bg-secondary/20 transition-all">
                  <input
                    type="checkbox"
                    checked={config.qualityGateEnabled}
                    onChange={(e) => updateConfigField('qualityGateEnabled', e.target.checked)}
                    className="h-5 w-5 rounded border-input text-primary focus:ring-primary"
                  />
                  <div className="flex-1">
                    <div className="font-medium group-hover:text-primary transition-colors">
                      启用质量门控
                    </div>
                    <div className="text-xs text-muted-foreground">
                      自动检测并过滤低质量因子，防止其进入下一轮迭代，保证最终结果质量
                    </div>
                  </div>
                </label>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Mining Direction Tab */}
        {activeTab === 'directions' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Compass className="h-5 w-5" />
                挖掘方向（参考 Alpha158(20)）
              </CardTitle>
              <p className="text-sm text-muted-foreground mt-1">
                选择作为默认参考的挖掘方向；启动任务时可从中选用或随机一条
              </p>
            </CardHeader>
            <CardContent className="space-y-6">
              <div>
                <label className="block text-sm font-medium mb-3">使用方式</label>
                <div className="flex flex-wrap gap-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="miningDirectionMode"
                      checked={config.miningDirectionMode === 'selected'}
                      onChange={() => updateConfigField('miningDirectionMode', 'selected')}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span>使用下方选中的方向（启动时从选中中取一条或按业务逻辑使用）</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="miningDirectionMode"
                      checked={config.miningDirectionMode === 'random'}
                      onChange={() => updateConfigField('miningDirectionMode', 'random')}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span className="flex items-center gap-1.5">
                      <Shuffle className="h-4 w-4" />
                      随机（从选中方向中随机选一条）
                    </span>
                  </label>
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-3">
                  <label className="text-sm font-medium">参考方向（可多选）</label>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        updateConfigField(
                          'selectedMiningDirectionIndices',
                          REFERENCE_MINING_DIRECTIONS.map((_: MiningDirectionItem, i: number) => i)
                        );
                      }}
                    >
                      全选
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => updateConfigField('selectedMiningDirectionIndices', [])}
                    >
                      取消全选
                    </Button>
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-[320px] overflow-y-auto rounded-lg border border-border/50 bg-secondary/10 p-3">
                  {REFERENCE_MINING_DIRECTIONS.map((item: MiningDirectionItem, idx: number) => {
                    const label = getDirectionLabel(item);
                    return (
                      <label
                        key={idx}
                        className="flex items-center gap-2 p-2 rounded-lg hover:bg-secondary/20 cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={config.selectedMiningDirectionIndices.includes(idx)}
                          onChange={(e) => {
                            const next = e.target.checked
                              ? [...config.selectedMiningDirectionIndices, idx].sort((a, b) => a - b)
                              : config.selectedMiningDirectionIndices.filter((i) => i !== idx);
                            updateConfigField('selectedMiningDirectionIndices', next);
                          }}
                          className="h-4 w-4 rounded border-input text-primary focus:ring-primary"
                        />
                        <span className="text-sm truncate flex-1" title={label}>
                          {label}
                        </span>
                      </label>
                    );
                  })}
                </div>
                <p className="text-xs text-muted-foreground mt-2">
                  已选 {config.selectedMiningDirectionIndices.length} / {REFERENCE_MINING_DIRECTIONS.length} 项。
                </p>
              </div>
            </CardContent>
          </Card>
        )}
        {activeTab === 'environment' && (
          <Card className="min-w-0">
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <CardTitle>环境检查与使用说明</CardTitle>
                <Button variant="outline" onClick={refreshEnvironment} disabled={checkingEnvironment}>
                  {checkingEnvironment ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-2" />}
                  刷新检查
                </Button>
              </div>
              <p className="text-sm text-muted-foreground">检查后端当前环境与已保存配置；不会调用外部模型，也不会验证 API 鉴权。</p>
            </CardHeader>
            <CardContent className="min-w-0 space-y-6">
              {environmentError && <p role="alert" className="text-sm text-destructive">{environmentError}</p>}
              {checkingEnvironment && <p role="status" className="text-sm text-muted-foreground">正在检查已保存环境…</p>}
              {environment && (
                <div className="space-y-3">
                  <p className="text-sm break-words">Python {environment.pythonVersion} · 运行环境：{environment.environmentName}</p>
                  <ul className="divide-y divide-border">
                    {environment.checks.map((item) => (
                      <li key={item.name} className="flex items-start gap-3 py-3 min-w-0">
                        {item.passed ? <Check className="h-4 w-4 text-success mt-0.5 shrink-0" /> : <AlertCircle className="h-4 w-4 text-warning mt-0.5 shrink-0" />}
                        <div className="min-w-0 text-sm">
                          <span className="font-medium">{item.name} · {item.passed ? '通过' : '待处理'}</span>
                          <p className="text-muted-foreground break-words">{item.detail}</p>
                        </div>
                      </li>
                    ))}
                  </ul>
                  <p className="text-xs text-muted-foreground break-words">{environment.scope}</p>
                </div>
              )}
              <section className="border-t border-border pt-5 space-y-3">
                <h3 className="font-medium">第一次使用</h3>
                <ol className="list-decimal pl-5 space-y-3 text-sm leading-relaxed">
                  <li>准备 Python / Conda 环境与项目依赖，再启动后端和前端。环境名称及安装命令见完整使用说明。</li>
                  <li>在“配置 API”填写自己的密钥、服务地址、聊天模型及推理模型 ID；两个模型 ID 可以相同，留空密钥会保留已保存的值。</li>
                  <li>自行准备 Qlib 数据，填写后端电脑上的数据目录及结果输出目录。</li>
                  <li>点击“保存配置”，再刷新本页检查。保存失败时更改会保留，检查只读取已保存的值。</li>
                  <li>选择挖掘方向与参数后开始新任务，再到“因子库”和“回测”查看结果。启动外部模型任务前请确认服务商的权限和费用。</li>
                </ol>
                <div className="flex flex-wrap gap-4 text-sm">
                  <a className="text-primary underline underline-offset-4" href="/api/v1/system/guide" target="_blank" rel="noreferrer">完整使用说明</a>
                  <a className="text-primary underline underline-offset-4" href="/api/v1/system/handoff" target="_blank" rel="noreferrer">交接文档 HANDOFF</a>
                </div>
                <p className="text-sm text-muted-foreground">当前工程恢复和自动影子盘状态以交接文档为准；环境检查通过不等于自动交易验证或盈利证明。</p>
              </section>
            </CardContent>
          </Card>
        )}
      </fieldset>

      {/* Info Footer */}
      <Card className="glass border-primary/20 bg-primary/5">
        <CardContent className="p-4 flex gap-3">
          <div className="text-xl">💡</div>
          <div className="text-sm text-muted-foreground">
            <p className="mb-1 font-medium text-foreground">配置提示</p>
            <p>修改后需点击“保存配置”。API、模型和数据路径写入后端环境文件，密钥不进入浏览器缓存；默认参数和挖掘方向仅作为当前浏览器的新任务偏好。已经运行的任务继续使用启动时配置，Python / Conda 环境变更需重新启动服务。</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};
