"use client";

import type { ComponentProps } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, CheckCircle2, CircleAlert, Clock3, LoaderCircle, Pause, Play, RefreshCw, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchRealtimeMonitor, type RealtimeMonitorEvent, type RealtimeMonitorMetricMap, type RealtimeMonitorRecord, type RealtimeMonitorResponse } from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

type BadgeVariant = ComponentProps<typeof Badge>["variant"];

const REFRESH_INTERVAL_STORAGE_KEY = "chatgpt2api_monitor_refresh_interval_secs";
const DEFAULT_REFRESH_INTERVAL_SECONDS = 5;
const MIN_REFRESH_INTERVAL_SECONDS = 1;
const MAX_REFRESH_INTERVAL_SECONDS = 300;

const stageLabels: Record<string, string> = {
  queued: "排队中",
  running: "任务执行中",
  getting_account: "分配账号",
  acquiring_account: "分配账号",
  account_ready: "账号就绪",
  account_selected: "账号已分配",
  uploading: "上传参考图",
  bootstrapping: "初始化上游会话",
  getting_token: "获取上游令牌",
  preparing_conversation: "准备生图会话",
  starting_generation: "提交生图请求",
  generating: "上游生成中",
  resolving: "解析结果",
  resolving_image: "解析图片结果",
  receiving_image: "接收图片结果",
  image_ready: "图片已就绪",
  upstream_message: "接收上游文本回复",
  retrying_account: "切换账号重试",
  account_retries_exhausted: "账号重试已耗尽",
  rejected: "请求被拒绝",
  completed: "已完成",
  failed: "失败",
};

const metricFallbackLabels: Record<string, string> = {
  handler_queue_ms: "入口排队",
  handler_exec_ms: "入口执行",
  stream_first_queue_ms: "首包排队",
  stream_first_exec_ms: "首包读取",
  account_wait_ms: "账号等待",
  egress_wait_ms: "出口等待",
  upload_ms: "图片上传",
  bootstrap_ms: "上游初始化",
  requirements_ms: "获取令牌",
  prepare_conversation_ms: "准备会话",
  generation_start_ms: "等待首包",
  sse_stream_ms: "上游流耗时",
  conversation_stream_ms: "上游生成",
  stream_error_ms: "上游断流",
  poll_wait_ms: "等待结果",
  poll_request_ms: "查询结果",
  resolve_ms: "结果处理",
  download_ms: "图片下载",
  stream_ms: "单图处理",
  total_ms: "单图总耗时",
};

const diagnosticMetricKeys = [
  "handler_queue_ms",
  "handler_exec_ms",
  "stream_first_queue_ms",
  "stream_first_exec_ms",
  "account_wait_ms",
  "egress_wait_ms",
  "upload_ms",
  "bootstrap_ms",
  "requirements_ms",
  "prepare_conversation_ms",
  "generation_start_ms",
  "sse_stream_ms",
  "conversation_stream_ms",
  "stream_error_ms",
  "poll_wait_ms",
  "poll_request_ms",
  "resolve_ms",
  "download_ms",
  "stream_ms",
  "total_ms",
];

function formatDuration(value?: number) {
  const milliseconds = Math.max(0, Number(value) || 0);
  if (milliseconds < 1000) {
    return `${milliseconds} ms`;
  }
  const seconds = milliseconds / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)} s`;
  }
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function clampRefreshInterval(value: unknown) {
  const seconds = Math.round(Number(value || DEFAULT_REFRESH_INTERVAL_SECONDS));
  if (!Number.isFinite(seconds)) {
    return DEFAULT_REFRESH_INTERVAL_SECONDS;
  }
  return Math.min(MAX_REFRESH_INTERVAL_SECONDS, Math.max(MIN_REFRESH_INTERVAL_SECONDS, seconds));
}

function readStoredRefreshInterval() {
  try {
    return clampRefreshInterval(window.localStorage.getItem(REFRESH_INTERVAL_STORAGE_KEY));
  } catch {
    return DEFAULT_REFRESH_INTERVAL_SECONDS;
  }
}

function formatMetric(value?: number) {
  const milliseconds = Number(value);
  return Number.isFinite(milliseconds) && milliseconds > 0 ? formatDuration(milliseconds) : "-";
}

function formatTime(value?: string) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function stageLabel(stage?: string, label?: string) {
  if (label) {
    return label;
  }
  if (!stage) {
    return "处理中";
  }
  return stageLabels[stage] || stage;
}

function recordStatus(record: RealtimeMonitorRecord) {
  if (record.status?.toLowerCase() === "success") {
    return { label: "成功", variant: "success" as BadgeVariant };
  }
  if (record.status?.toLowerCase() === "text_review") {
    return { label: "文本回复", variant: "warning" as BadgeVariant };
  }
  if (["failed", "error", "fail"].includes(record.status?.toLowerCase() || "")) {
    return { label: "失败", variant: "danger" as BadgeVariant };
  }
  if (record.stage === "retrying_account") {
    return { label: "重试中", variant: "warning" as BadgeVariant };
  }
  return { label: "运行中", variant: "info" as BadgeVariant };
}

function eventDescription(event: RealtimeMonitorEvent) {
  if (event.label) {
    return event.label;
  }
  if (event.event === "started") {
    return "请求已进入处理队列";
  }
  if (event.event === "finished") {
    return "请求已结束";
  }
  return stageLabel(event.stage || event.event);
}

function accountAttempt(record: RealtimeMonitorRecord) {
  const maxAttempts = record.image_account_max_attempts ?? record.max_attempts;
  const attempt = record.image_account_attempt ?? record.attempt;
  if (maxAttempts && maxAttempts > 0) {
    return `${attempt || 1}/${maxAttempts}`;
  }
  const switches = record.image_account_switch_count ?? record.retry_count;
  return switches && switches > 0 ? `已切换 ${switches}` : "-";
}

function metricLabel(key: string, labels: Record<string, string>) {
  return labels[key] || metricFallbackLabels[key] || key.replace(/_ms$/, "");
}

function metricValue(record: RealtimeMonitorRecord, key: string) {
  return Math.max(Number(record.metrics?.[key]) || 0, Number(record.perf?.[key]) || 0);
}

function metricDigest(record: RealtimeMonitorRecord, labels: Record<string, string>, limit = 2) {
  const items = diagnosticMetricKeys
    .map((key) => ({ key, value: metricValue(record, key) }))
    .filter((item) => item.value > 0)
    .sort((left, right) => right.value - left.value)
    .slice(0, limit);
  return items.length ? items.map((item) => `${metricLabel(item.key, labels)} ${formatDuration(item.value)}`).join(" · ") : "-";
}

function recordEgress(record: RealtimeMonitorRecord) {
  const values = [record.egress_label, record.proxy_source, record.proxy_hash]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return Array.from(new Set(values)).join(" / ") || "-";
}

function recordFailure(record: RealtimeMonitorRecord) {
  return record.error || record.public_error || record.upstream_error || record.raw_error || record.local_reason || record.code || "-";
}

function MonitorContent() {
  const [data, setData] = useState<RealtimeMonitorResponse | null>(null);
  const [error, setError] = useState("");
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshIntervalSeconds, setRefreshIntervalSeconds] = useState(DEFAULT_REFRESH_INTERVAL_SECONDS);
  const loadingRef = useRef(false);

  const load = useCallback(async () => {
    if (loadingRef.current) {
      return;
    }
    loadingRef.current = true;
    setIsRefreshing(true);
    try {
      const next = await fetchRealtimeMonitor();
      setData(next);
      setError("");
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "加载实时监控失败");
    } finally {
      loadingRef.current = false;
      setIsRefreshing(false);
    }
  }, []);

  const updateRefreshInterval = useCallback((value: unknown) => {
    const nextValue = clampRefreshInterval(value);
    setRefreshIntervalSeconds(nextValue);
    try {
      window.localStorage.setItem(REFRESH_INTERVAL_STORAGE_KEY, String(nextValue));
    } catch {
      // Storage can be unavailable in private or restricted browser contexts.
    }
  }, []);

  useEffect(() => {
    setRefreshIntervalSeconds(readStoredRefreshInterval());
  }, []);

  useEffect(() => {
    let interval: number | undefined;

    const stop = () => {
      if (interval) {
        window.clearInterval(interval);
        interval = undefined;
      }
    };

    const start = () => {
      stop();
      if (!autoRefresh || document.visibilityState !== "visible") {
        return;
      }
      void load();
      interval = window.setInterval(() => void load(), refreshIntervalSeconds * 1000);
    };

    const onVisibilityChange = () => start();
    start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [autoRefresh, load, refreshIntervalSeconds]);

  const summary = data?.summary;
  const accounts = data?.accounts;
  const active = data?.active || [];
  const recent = data?.recent || [];
  const slow = data?.slow || [];
  const events = data?.events || [];
  const monitorWindow = data?.window;
  const metricLabels = data?.metric_labels ?? {};
  const metricP95: RealtimeMonitorMetricMap = summary?.metric_p95 ?? {};
  const p95Metrics = diagnosticMetricKeys
    .map((key) => ({ key, label: metricLabel(key, metricLabels), value: Number(metricP95[key]) || 0 }))
    .filter((item) => item.value > 0)
    .sort((left, right) => right.value - left.value)
    .slice(0, 6);
  const metrics = [
    { label: "运行中", value: summary?.active ?? 0, icon: Activity, color: "text-sky-700" },
    { label: "成功率", value: `${summary?.success_rate ?? 0}%`, icon: CheckCircle2, color: "text-emerald-700" },
    { label: "账号切换", value: summary?.account_switches ?? 0, icon: RotateCcw, color: "text-amber-700" },
    { label: "可用账号", value: accounts?.active ?? 0, icon: Activity, color: "text-violet-700" },
    { label: "可用额度", value: accounts?.total_quota ?? 0, icon: Clock3, color: "text-stone-700" },
  ];

  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">Monitor</div>
          <h1 className="text-2xl font-semibold tracking-tight">实时运行状态</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm text-stone-500">
          <span>更新于 {formatTime(data?.updated_at)}</span>
          <Badge variant={autoRefresh ? "success" : "secondary"} className="rounded-md">
            {autoRefresh ? "自动刷新" : "已暂停"}
          </Badge>
          <label className="flex items-center gap-1.5 text-xs text-stone-500">
            <span className="whitespace-nowrap">间隔</span>
            <Input
              type="number"
              min={MIN_REFRESH_INTERVAL_SECONDS}
              max={MAX_REFRESH_INTERVAL_SECONDS}
              step={1}
              value={refreshIntervalSeconds}
              aria-label="自动刷新间隔（秒）"
              className="h-8 w-16 rounded-md px-2 text-center tabular-nums"
              onChange={(event) => updateRefreshInterval(event.target.value)}
            />
            <span>秒</span>
          </label>
          <Button
            type="button"
            variant="outline"
            size="icon"
            title={autoRefresh ? "暂停自动刷新" : "继续自动刷新"}
            aria-label={autoRefresh ? "暂停自动刷新" : "继续自动刷新"}
            className="rounded-md border-stone-200 bg-white text-stone-600"
            onClick={() => setAutoRefresh((current) => !current)}
          >
            {autoRefresh ? <Pause className="size-4" /> : <Play className="size-4" />}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon"
            title="立即刷新"
            aria-label="立即刷新"
            className="rounded-md border-stone-200 bg-white text-stone-600"
            onClick={() => void load()}
            disabled={isRefreshing}
          >
            <RefreshCw className={`size-4 ${isRefreshing ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </div>

      {error ? (
        <div role="alert" className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          <CircleAlert className="size-4 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {!data && isRefreshing ? (
        <div className="flex min-h-60 items-center justify-center">
          <LoaderCircle className="size-5 animate-spin text-stone-400" />
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            {metrics.map((metric) => {
              const Icon = metric.icon;
              return (
                <Card key={metric.label} className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
                  <CardContent className="p-4">
                    <div className="flex items-center justify-between gap-3 text-sm text-stone-500">
                      <span>{metric.label}</span>
                      <Icon className={`size-4 ${metric.color}`} />
                    </div>
                    <div className={`mt-3 text-2xl font-semibold ${metric.color}`}>{metric.value}</div>
                  </CardContent>
                </Card>
              );
            })}
          </div>

          <div className="grid gap-5 xl:grid-cols-2">
            <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
              <CardContent className="p-5">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="text-base font-semibold text-stone-900">运行窗口</h2>
                  <span className="text-xs text-stone-500">进程内统计</span>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-3">
                  <div>
                    <p className="text-xs text-stone-500">完成记录</p>
                    <p className="mt-1 font-mono text-lg font-semibold text-stone-900">
                      {monitorWindow?.completed ?? summary?.completed ?? 0}{monitorWindow?.completed_capacity ? ` / ${monitorWindow.completed_capacity}` : ""}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-stone-500">阶段事件</p>
                    <p className="mt-1 font-mono text-lg font-semibold text-stone-900">
                      {monitorWindow?.events ?? events.length}{monitorWindow?.event_capacity ? ` / ${monitorWindow.event_capacity}` : ""}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-stone-500">入口线程容量</p>
                    <p className="mt-1 font-mono text-lg font-semibold text-stone-900">{data?.threadpool?.tokens ?? "-"}</p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
              <CardContent className="p-5">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="text-base font-semibold text-stone-900">P95 耗时</h2>
                  <span className="font-mono text-xs text-stone-500">总计 {formatMetric(summary?.p95_duration_ms)}</span>
                </div>
                {p95Metrics.length ? (
                  <div className="mt-4 grid gap-3 sm:grid-cols-3">
                    {p95Metrics.map((metric) => (
                      <div key={metric.key}>
                        <p className="truncate text-xs text-stone-500" title={metric.label}>{metric.label}</p>
                        <p className="mt-1 font-mono text-sm font-semibold text-stone-900">{formatDuration(metric.value)}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="mt-4 text-sm text-stone-500">当前后端尚未上报阶段 P95 指标。</p>
                )}
              </CardContent>
            </Card>
          </div>

          <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
            <CardContent className="p-0">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-100 px-5 py-4">
                <div>
                  <h2 className="text-base font-semibold text-stone-900">正在处理</h2>
                  <p className="mt-1 text-sm text-stone-500">{active.length} 个请求</p>
                </div>
                <Badge variant="info" className="rounded-md">实时</Badge>
              </div>
              <div className="overflow-x-auto">
                <Table className="min-w-[980px]">
                  <TableHeader>
                    <TableRow>
                      <TableHead>阶段</TableHead>
                      <TableHead>模型</TableHead>
                      <TableHead>账号</TableHead>
                      <TableHead>尝试</TableHead>
                      <TableHead>已耗时</TableHead>
                      <TableHead>关键耗时</TableHead>
                      <TableHead>调用方</TableHead>
                      <TableHead>状态</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {active.map((record) => {
                      const status = recordStatus(record);
                      return (
                        <TableRow key={record.call_id} className="text-stone-600">
                          <TableCell className="font-medium text-stone-800">{stageLabel(record.stage, record.stage_label)}</TableCell>
                          <TableCell className="max-w-48 truncate" title={record.model}>{record.model || "-"}</TableCell>
                          <TableCell className="max-w-56 truncate" title={record.account_email}>{record.account_email || "等待分配"}</TableCell>
                          <TableCell>{accountAttempt(record)}</TableCell>
                          <TableCell className="font-mono text-xs">{formatDuration(record.elapsed_ms)}</TableCell>
                          <TableCell className="max-w-56 truncate font-mono text-xs text-stone-500" title={metricDigest(record, metricLabels)}>{metricDigest(record, metricLabels)}</TableCell>
                          <TableCell className="max-w-44 truncate text-stone-500" title={record.key_name}>{record.key_name || "-"}</TableCell>
                          <TableCell><Badge variant={status.variant} className="rounded-md">{status.label}</Badge></TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
              {active.length === 0 ? <div className="px-5 py-12 text-center text-sm text-stone-500">当前没有运行中的请求</div> : null}
            </CardContent>
          </Card>

          <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
            <CardContent className="p-0">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-100 px-5 py-4">
                <div>
                  <h2 className="text-base font-semibold text-stone-900">慢请求</h2>
                  <p className="mt-1 text-sm text-stone-500">按入口、账号、出口、上游生成和结果处理耗时排序。</p>
                </div>
                <Badge variant={slow.length ? "warning" : "secondary"} className="rounded-md">{slow.length}</Badge>
              </div>
              {slow.length ? (
                <div className="overflow-x-auto">
                  <Table className="min-w-[940px]">
                    <TableHeader>
                      <TableRow>
                        <TableHead>模型</TableHead>
                        <TableHead>阶段</TableHead>
                        <TableHead>总耗时</TableHead>
                        <TableHead>关键耗时</TableHead>
                        <TableHead>账号 / 出口</TableHead>
                        <TableHead>原因</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {slow.slice(0, 20).map((record) => (
                        <TableRow key={`${record.call_id}-${record.ended_at || record.updated_at || "slow"}`} className="text-stone-600">
                          <TableCell className="max-w-40 truncate" title={record.model}>{record.model || "-"}</TableCell>
                          <TableCell className="max-w-44 truncate" title={stageLabel(record.stage, record.stage_label)}>{stageLabel(record.stage, record.stage_label)}</TableCell>
                          <TableCell className="font-mono text-xs">{formatMetric(record.duration_ms)}</TableCell>
                          <TableCell className="max-w-64 truncate font-mono text-xs text-stone-500" title={metricDigest(record, metricLabels, 3)}>{metricDigest(record, metricLabels, 3)}</TableCell>
                          <TableCell className="max-w-56 truncate" title={`${record.account_email || "-"} / ${recordEgress(record)}`}>{record.account_email || "-"} / {recordEgress(record)}</TableCell>
                          <TableCell className="max-w-72 truncate text-rose-600" title={recordFailure(record)}>{recordFailure(record)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              ) : <div className="px-5 py-10 text-center text-sm text-stone-500">当前后端没有返回慢请求记录。</div>}
            </CardContent>
          </Card>

          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.8fr)]">
            <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
              <CardContent className="p-0">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-100 px-5 py-4">
                  <div>
                    <h2 className="text-base font-semibold text-stone-900">最近完成</h2>
                    <p className="mt-1 text-sm text-stone-500">成功 {summary?.success ?? 0}，失败 {summary?.failed ?? 0}</p>
                  </div>
                </div>
                <div className="overflow-x-auto">
                  <Table className="min-w-[820px]">
                    <TableHeader>
                      <TableRow>
                        <TableHead>时间</TableHead>
                        <TableHead>模型</TableHead>
                        <TableHead>账号</TableHead>
                        <TableHead>切换</TableHead>
                        <TableHead>耗时</TableHead>
                        <TableHead>结果</TableHead>
                        <TableHead>错误</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {recent.slice(0, 30).map((record) => {
                        const status = recordStatus(record);
                        return (
                          <TableRow key={record.call_id} className="text-stone-600">
                            <TableCell className="whitespace-nowrap text-stone-500">{formatTime(record.ended_at || record.updated_at)}</TableCell>
                            <TableCell className="max-w-40 truncate" title={record.model}>{record.model || "-"}</TableCell>
                            <TableCell className="max-w-52 truncate" title={record.account_email}>{record.account_email || "-"}</TableCell>
                            <TableCell>{record.retry_count || 0}</TableCell>
                            <TableCell className="font-mono text-xs">{formatDuration(record.duration_ms)}</TableCell>
                            <TableCell><Badge variant={status.variant} className="rounded-md">{status.label}</Badge></TableCell>
                            <TableCell className="max-w-64 truncate text-rose-600" title={recordFailure(record)}>{recordFailure(record)}</TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
                {recent.length === 0 ? <div className="px-5 py-12 text-center text-sm text-stone-500">尚无已完成的请求</div> : null}
              </CardContent>
            </Card>

            <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
              <CardContent className="p-0">
                <div className="border-b border-stone-100 px-5 py-4">
                  <h2 className="text-base font-semibold text-stone-900">最新事件</h2>
                </div>
                <div className="max-h-[530px] divide-y divide-stone-100 overflow-y-auto">
                  {events.slice(0, 40).map((event) => (
                    <div key={`${event.time}-${event.call_id}-${event.event}`} className="space-y-1 px-5 py-3">
                      <div className="flex items-center justify-between gap-3 text-sm">
                        <span className="font-medium text-stone-800">{eventDescription(event)}</span>
                        <span className="shrink-0 font-mono text-xs text-stone-400">{formatTime(event.time)}</span>
                      </div>
                      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-stone-500">
                        {event.model ? <span>{event.model}</span> : null}
                        {event.account_email ? <span>{event.account_email}</span> : null}
                        {event.previous_account_email ? <span>{event.previous_account_email} → {event.account_email || "下一账号"}</span> : null}
                        {event.retry_count ? <span>切换 {event.retry_count}</span> : null}
                      </div>
                      {event.error || event.code ? <p className="line-clamp-2 text-xs leading-5 text-rose-600">{event.error || event.code}</p> : null}
                    </div>
                  ))}
                </div>
                {events.length === 0 ? <div className="px-5 py-12 text-center text-sm text-stone-500">尚无监控事件</div> : null}
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </section>
  );
}

export default function MonitorPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <MonitorContent />;
}
