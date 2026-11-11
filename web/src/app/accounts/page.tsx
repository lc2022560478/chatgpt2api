"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
import {
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleOff,
  Copy,
  Download,
  KeyRound,
  Link2,
  LoaderCircle,
  LogIn,
  Pencil,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteAccounts,
  fetchAccounts,
  fetchModels,
  fetchRefreshProgress,
  fetchReLoginProgress,
  reLoginAccounts,
  refreshAccountTokens,
  refreshAccounts,
  testProxy,
  updateAccount,
  waitForRefreshProgress,
  type Account,
  type AccountRefreshResponse,
  type AccountStatus,
  type Model,
  type RefreshProgressResponse,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

import { AccountImportDialog } from "./components/account-import-dialog";

const accountStatusOptions: { label: string; value: AccountStatus | "all" }[] = [
  { label: "全部状态", value: "all" },
  { label: "正常", value: "正常" },
  { label: "限流", value: "限流" },
  { label: "异常", value: "异常" },
  { label: "禁用", value: "禁用" },
];

const statusMeta: Record<
  AccountStatus,
  {
    icon: typeof CheckCircle2;
    badge: ComponentProps<typeof Badge>["variant"];
  }
> = {
  正常: { icon: CheckCircle2, badge: "success" },
  限流: { icon: CircleAlert, badge: "warning" },
  异常: { icon: CircleOff, badge: "danger" },
  禁用: { icon: Ban, badge: "secondary" },
};

const metricCards = [
  { key: "total", label: "账户总数", color: "text-stone-900", icon: UserRound },
  { key: "active", label: "正常账户", color: "text-emerald-600", icon: CheckCircle2 },
  { key: "limited", label: "限流账户", color: "text-orange-500", icon: CircleAlert },
  { key: "abnormal", label: "异常账户", color: "text-rose-500", icon: CircleOff },
  { key: "disabled", label: "禁用账户", color: "text-stone-500", icon: Ban },
  { key: "quota", label: "剩余额度", color: "text-blue-500", icon: RefreshCw },
] as const;

function formatCompact(value: number) {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}

function formatQuota(account: Account) {
  return String(Math.max(0, account.quota));
}

function formatRestoreAt(value?: string | null, now = Date.now()) {
  if (!value) {
    return { absolute: "—", relative: "" };
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return { absolute: value, relative: "" };
  }

  const diffMs = Math.max(0, date.getTime() - now);
  const totalHours = Math.ceil(diffMs / (1000 * 60 * 60));
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  const relative = diffMs > 0 ? `剩余 ${days}天 ${hours}小时` : "已到恢复时间";
  const absolute = tokenExpiryFormatter.format(date).replaceAll("/", "-");

  return { absolute, relative };
}

function formatQuotaSummary(accounts: Account[]) {
  const availableAccounts = accounts.filter((account) => account.status === "正常");
  return formatCompact(availableAccounts.reduce((sum, account) => sum + Math.max(0, account.quota), 0));
}

const tokenExpiryFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function TimestampText({ absolute, value, compact = false }: { absolute: string; value?: string | null; compact?: boolean }) {
  return (
    <time dateTime={value ?? undefined} className={cn("text-xs leading-5 text-stone-500 tabular-nums", compact ? "flex flex-col" : "block whitespace-nowrap")}>
      {compact ? absolute.split(" ").map((part, index) => <span key={index}>{part}</span>) : absolute}
    </time>
  );
}

function TokenExpiry({ token, value, now, compact = false }: { token?: string | null; value?: string | null; now: number; compact?: boolean }) {
  if (!token?.trim()) {
    return <span className="text-xs text-stone-400">未绑定</span>;
  }
  const expiresAt = value ? new Date(value).getTime() : NaN;
  if (!Number.isFinite(expiresAt)) {
    return <span className="text-xs text-stone-400" title="Token 和上游响应未提供明确到期时间">未知</span>;
  }
  const remaining = expiresAt - now;
  const totalMinutes = Math.max(1, Math.ceil(remaining / 60_000));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  const relative = remaining <= 0 ? "已到期"
    : days > 0 ? `剩余 ${days}天 ${hours}小时`
    : hours > 0 ? `剩余 ${hours}小时 ${minutes}分钟`
    : `剩余 ${minutes}分钟`;
  const absolute = tokenExpiryFormatter.format(new Date(expiresAt)).replaceAll("/", "-");
  return (
    <div className="space-y-0.5 whitespace-nowrap text-xs leading-5 tabular-nums" title={`${absolute}（北京时间）`}>
      <div className={cn("font-medium", remaining <= 0 ? "text-rose-600" : remaining <= 86_400_000 ? "text-amber-600" : "text-stone-700")}>{relative}</div>
      <TimestampText absolute={absolute} value={value} compact={compact} />
    </div>
  );
}

function maskToken(token?: string) {
  if (!token) return "—";
  if (token.length <= 18) return token;
  return `${token.slice(0, 16)}...${token.slice(-8)}`;
}

function downloadTokens(accounts: Account[]) {
  const content = `${accounts.map((account) => account.access_token).join("\n")}\n`;
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `accounts-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function displayAccountType(account: Account) {
  return account.type || "Free";
}

function displayAccountSource(account: Account) {
  const source = String(account.source_type || "").trim().toLowerCase();
  if (!source) {
    return "web";
  }
  if (source === "web") {
    return "web";
  }
  return source;
}

function AccountIdentity({ account, compact = false }: { account: Account; compact?: boolean }) {
  const rawCreatedAt = account.created_at;
  const createdDate = rawCreatedAt ? new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(rawCreatedAt)
    ? rawCreatedAt : `${rawCreatedAt.replace(" ", "T")}Z`) : null;
  const createdAt = createdDate && Number.isFinite(createdDate.getTime())
    ? tokenExpiryFormatter.format(createdDate).replaceAll("/", "-") : null;
  return (
    <div className="min-w-0 space-y-0.5">
      <div className={cn("text-[13px] leading-5 font-medium text-stone-800", compact ? "break-all" : "truncate")} title={account.email || undefined}>
        {account.email || "未获取邮箱"}
      </div>
      <div className="flex min-w-0 items-center gap-1.5 text-xs text-stone-500">
        <span className="truncate font-mono">{maskToken(account.access_token)}</span>
        <button
          type="button"
          className={cn("inline-flex shrink-0 items-center justify-center rounded-md text-stone-400 transition hover:bg-stone-100 hover:text-stone-700", compact ? "size-6" : "size-5")}
          title="复制 AT"
          aria-label={`复制 AT ${account.email || maskToken(account.access_token)}`}
          onClick={() => {
            void navigator.clipboard.writeText(account.access_token);
            toast.success("token 已复制");
          }}
        >
          <Copy className="size-3.5" />
        </button>
      </div>
      <div className="truncate text-[11px] leading-4 text-stone-400" title={createdAt ? `创建于 ${createdAt}（北京时间）` : undefined}>
        创建 {createdAt ? createdAt.slice(5, 16) : "未知"}
      </div>
    </div>
  );
}

function AccountStatusBadge({ account }: { account: Account }) {
  const status = statusMeta[account.status];
  const Icon = status.icon;
  return (
    <Badge variant={status.badge} className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-1 text-xs">
      <Icon className="size-3.5" />
      {account.status}
    </Badge>
  );
}

function AccountUsage({ account }: { account: Account }) {
  const inflight = account.image_inflight ?? 0;
  return (
    <dl className="grid grid-cols-3 gap-1 text-center tabular-nums">
      {[
        { label: "在途", value: inflight, color: inflight > 0 ? "text-amber-600" : "text-stone-500" },
        { label: "成功", value: account.success, color: account.success > 0 ? "text-emerald-600" : "text-stone-500" },
        { label: "失败", value: account.fail, color: account.fail > 0 ? "text-rose-500" : "text-stone-500" },
      ].map((item) => (
        <div key={item.label} title={`${item.label}：${item.value}`}>
          <dt className="text-[11px] leading-5 text-stone-400">{item.label}</dt>
          <dd className={cn("text-xs font-medium leading-5", item.color)}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function AccountRestoreTime({ account, now, compact = false }: { account: Account; now: number; compact?: boolean }) {
  const cooldowns = [account.image_success_cooldown_until, account.image_quota_cooldown_until]
    .filter((value): value is string => Boolean(value) && Number.isFinite(Date.parse(value!)));
  const restoreAt = cooldowns.sort((a, b) => Date.parse(b) - Date.parse(a))[0] || account.restore_at;
  const restore = formatRestoreAt(restoreAt, now);
  return (
    <div className="space-y-0.5 whitespace-nowrap text-xs leading-5" title={restoreAt ? `${restore.absolute}（北京时间）` : undefined}>
      {restore.relative ? <div className="font-medium text-stone-700">{restore.relative}</div> : null}
      <TimestampText absolute={restore.absolute} value={restoreAt} compact={compact} />
    </div>
  );
}

function AccountsPageContent() {
  const didLoadRef = useRef(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [expiryNow, setExpiryNow] = useState(() => Date.now());
  const [availableModels, setAvailableModels] = useState<Model[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<AccountStatus | "all">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState("10");
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [editStatus, setEditStatus] = useState<AccountStatus>("正常");
  const [editProxy, setEditProxy] = useState("");
  const [editRefreshToken, setEditRefreshToken] = useState("");
  const [isEditingRefreshToken, setIsEditingRefreshToken] = useState(false);
  const editRefreshTokenRef = useRef<HTMLInputElement>(null);
  const [isTestingProxy, setIsTestingProxy] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingModels, setIsLoadingModels] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshingTokens, setRefreshingTokens] = useState<Set<string>>(new Set());
  const [isDeleting, setIsDeleting] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [isRelogining, setIsRelogining] = useState(false);
  const [isRefreshingAT, setIsRefreshingAT] = useState(false);
  const [atRefreshTargets, setAtRefreshTargets] = useState<Set<string>>(new Set());
  const [progress, setProgress] = useState<{
    visible: boolean;
    current: number;
    total: number;
    message: string;
    email: string;
  }>({
    visible: false,
    current: 0,
    total: 0,
    message: "",
    email: "",
  });
  const progressRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [refreshSummary, setRefreshSummary] = useState<Record<string, number | string> | null>(null);
  const accountActionsBusy = isRefreshingAT || isRefreshing || isRelogining || refreshingTokens.size > 0;

  const loadAccounts = async (silent = false) => {
    if (!silent) {
      setIsLoading(true);
    }
    try {
      const data = await fetchAccounts();
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载账户失败";
      toast.error(message);
    } finally {
      if (!silent) {
        setIsLoading(false);
      }
    }
  };

  const loadModels = async () => {
    setIsLoadingModels(true);
    try {
      const data = await fetchModels();
      setAvailableModels(Array.isArray(data.data) ? data.data : []);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载模型列表失败";
      toast.error(message);
    } finally {
      setIsLoadingModels(false);
    }
  };

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void loadAccounts();
    void loadModels();

    // 清理进度条定时器
    return () => {
      if (progressRef.current) clearInterval(progressRef.current);
    };
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setExpiryNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (isEditingRefreshToken) editRefreshTokenRef.current?.focus();
  }, [isEditingRefreshToken]);

  useEffect(() => {
    let cancelled = false;
    let pending = false;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible" && !pending && !accountActionsBusy && !isUpdating && !isDeleting && !editingAccount) {
        pending = true;
        void fetchAccounts().then((data) => {
          if (cancelled) return;
          setAccounts(data.items);
          setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
        }).catch(() => {
          // Background synchronization must not interrupt an active workflow.
        }).finally(() => { pending = false; });
      }
    }, 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [accountActionsBusy, isUpdating, isDeleting, editingAccount]);

  const filteredAccounts = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return accounts.filter((account) => {
      const searchMatched =
        normalizedQuery.length === 0 || (account.email ?? "").toLowerCase().includes(normalizedQuery);
      const typeMatched = typeFilter === "all" || displayAccountType(account) === typeFilter;
      const statusMatched = statusFilter === "all" || account.status === statusFilter;
      return searchMatched && typeMatched && statusMatched;
    });
  }, [accounts, query, statusFilter, typeFilter]);

  const pageCount = Math.max(1, Math.ceil(filteredAccounts.length / Number(pageSize)));
  const safePage = Math.min(page, pageCount);
  const startIndex = (safePage - 1) * Number(pageSize);
  const currentRows = filteredAccounts.slice(startIndex, startIndex + Number(pageSize));
  const allCurrentSelected =
    currentRows.length > 0 && currentRows.every((row) => selectedIds.includes(row.access_token));

  const summary = useMemo(() => {
    const total = accounts.length;
    const active = accounts.filter((item) => item.status === "正常").length;
    const limited = accounts.filter((item) => item.status === "限流").length;
    const abnormal = accounts.filter((item) => item.status === "异常").length;
    const disabled = accounts.filter((item) => item.status === "禁用").length;
    const quota = formatQuotaSummary(accounts);

    return { total, active, limited, abnormal, disabled, quota };
  }, [accounts]);

  const accountTypeOptions = useMemo(
    () => [
      { label: "全部类型", value: "all" },
      ...Array.from(new Set(accounts.map(displayAccountType))).map((type) => ({ label: type, value: type })),
    ],
    [accounts],
  );

  const selectedTokens = useMemo(() => {
    const selectedSet = new Set(selectedIds);
    return accounts.filter((item) => selectedSet.has(item.access_token)).map((item) => item.access_token);
  }, [accounts, selectedIds]);

  const abnormalTokens = useMemo(() => {
    return accounts.filter((item) => item.status === "异常").map((item) => item.access_token);
  }, [accounts]);

  const paginationItems = useMemo(() => {
    const items: (number | "...")[] = [];
    const start = Math.max(1, safePage - 1);
    const end = Math.min(pageCount, safePage + 1);

    if (start > 1) items.push(1);
    if (start > 2) items.push("...");
    for (let current = start; current <= end; current += 1) items.push(current);
    if (end < pageCount - 1) items.push("...");
    if (end < pageCount) items.push(pageCount);

    return items;
  }, [pageCount, safePage]);

  const handleDeleteTokens = async (tokens: string[]) => {
    if (tokens.length === 0) {
      toast.error("请先选择要删除的账户");
      return;
    }

    setIsDeleting(true);
    try {
      const data = await deleteAccounts(tokens);
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      toast.success(`删除 ${data.removed ?? 0} 个账户`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除账户失败";
      toast.error(message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleRefreshAccounts = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("没有需要刷新的账户");
      return;
    }

    if (accessTokens.length === 1) {
      setRefreshingTokens((prev) => new Set([...prev, accessTokens[0]]));
      try {
        const { progress_id } = await refreshAccounts(accessTokens);
        // 单账号：轮询等待完成
        await pollRefreshProgress(progress_id, (progress) => {
          if (progress.done && progress.result) {
            setAccounts(progress.result.items);
            setSelectedIds((prev) => prev.filter((id) => progress.result!.items.some((item) => item.access_token === id)));
          }
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "刷新账户失败";
        toast.error(message);
      } finally {
        setRefreshingTokens((prev) => {
          const next = new Set(prev);
          next.delete(accessTokens[0]);
          return next;
        });
      }
      return;
    }

    setIsRefreshing(true);

    // 计算非选中账号的基数（统计卡片联动用）
    const selectedTokenSet = new Set(accessTokens);
    const baseAccountsList = accounts.filter((a) => !selectedTokenSet.has(a.access_token));
    const baseActive = baseAccountsList.filter((a) => a.status === "正常").length;
    const baseLimited = baseAccountsList.filter((a) => a.status === "限流").length;
    const baseAbnormal = baseAccountsList.filter((a) => a.status === "异常").length;
    const baseDisabled = baseAccountsList.filter((a) => a.status === "禁用").length;
    const baseNormalAccounts = baseAccountsList.filter((a) => a.status === "正常");
    const baseQuotaNum = baseNormalAccounts.reduce((s, a) => s + Math.max(0, a.quota), 0);

    // 显示进度条（只显示当前任务，不含分类统计）
    const total = accessTokens.length;
    setProgress({
      visible: true,
      current: 0,
      total,
      message: "正在刷新账号信息...",
      email: "",
    });

    try {
      const { progress_id } = await refreshAccounts(accessTokens);

      // 轮询进度到完成
      const data = await new Promise<AccountRefreshResponse>((resolve, reject) => {
        const pollTimer = setInterval(async () => {
          try {
            const p = await fetchRefreshProgress(progress_id);
            if (p.done) {
              clearInterval(pollTimer);
              if (p.error) {
                reject(new Error(p.error));
                return;
              }
              if (!p.result) {
                reject(new Error("刷新结果为空"));
                return;
              }
              // 更新最终进度显示
              setProgress((prev) => ({
                ...prev,
                current: prev.total,
                message: "刷新完成",
              }));
              // 清除联动统计
              setRefreshSummary(null);
              resolve(p.result);
            } else {
              // 实时更新进度
              setProgress((prev) => ({
                ...prev,
                current: p.processed,
              }));
              // 实时更新统计卡片：基数 + 已刷新的累加结果
              const runningActive = baseActive + ((p.status_counts?.["正常"]) ?? 0);
              const runningLimited = baseLimited + ((p.status_counts?.["限流"]) ?? 0);
              const runningAbnormal = baseAbnormal + ((p.status_counts?.["异常"]) ?? 0);
              const runningDisabled = baseDisabled + ((p.status_counts?.["禁用"]) ?? 0);
              setRefreshSummary({
                total: accounts.length,
                active: runningActive,
                limited: runningLimited,
                abnormal: runningAbnormal,
                disabled: runningDisabled,
                quota: formatCompact(baseQuotaNum + (p.total_quota ?? 0)),
              });
            }
          } catch (err) {
            clearInterval(pollTimer);
            reject(err);
          }
        }, 300);
      });

      // 刷新完成，更新数据
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));

      const relogined = data.relogined ?? 0;

      // 显示重新登录进度
      if (relogined > 0) {
        setProgress({
          visible: true,
          current: 0,
          total: relogined,
          message: `正在尝试对 ${relogined} 个账号进行移除异常状态`,
          email: "",
        });
        // 模拟重新登录进度
        let reCount = 0;
        await new Promise<void>((resolve) => {
          const timer = setInterval(() => {
            reCount += 1;
            if (reCount >= relogined) {
              clearInterval(timer);
              setProgress({
                visible: true,
                current: relogined,
                total: relogined,
                message: "移除异常状态完成",
                email: "",
              });
              setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);
              resolve();
            } else {
              setProgress((prev) => ({ ...prev, current: reCount }));
            }
          }, 150);
          setTimeout(resolve, 2000);
        });
      } else {
        setProgress({
          visible: true,
          current: total,
          total,
          message: "刷新完成",
          email: "",
        });
        setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);
      }

      if ((data.errors ?? []).length > 0) {
        const firstError = data.errors?.[0]?.error;
        toast.error(
          `刷新成功 ${data.refreshed} 个，失败 ${(data.errors ?? []).length} 个${firstError ? `，首个错误：${firstError}` : ""}`,
        );
      } else {
        toast.success(`刷新成功 ${data.refreshed} 个账户${relogined > 0 ? `，已触发 ${relogined} 个账号重新登录` : ""}`);
      }
    } catch (error) {
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
      setRefreshSummary(null);
      const message = error instanceof Error ? error.message : "刷新账户失败";
      toast.error(message);
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleRefreshAT = async (accessTokens: string[]) => {
    if (!accessTokens.some((token) => accounts.find((account) => account.access_token === token)?.refresh_token?.trim())) {
      toast.error("这些账号没有 RT，请先在编辑账户中绑定 Refresh Token");
      return;
    }
    setIsRefreshingAT(true);
    setAtRefreshTargets(new Set(accessTokens));
    setProgress({ visible: true, current: 0, total: accessTokens.length, message: "正在用 RT 刷新 AT...", email: "" });
    try {
      const { progress_id } = await refreshAccountTokens(accessTokens);
      const result = await waitForRefreshProgress(progress_id, (next) => {
        setProgress((prev) => ({ ...prev, current: next.processed, total: next.total }));
      });
      setAccounts(result.items);
      setSelectedIds([]);
      const message = `AT 刷新成功 ${result.refreshed} 个，跳过 ${result.skipped ?? 0} 个无 RT 账号，失败 ${result.errors.length} 个`;
      if (result.errors.length > 0) {
        toast.error(`${message}，首个错误：${result.errors[0].error}`);
      } else {
        toast.success(message);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "RT 刷新 AT 失败");
    } finally {
      setIsRefreshingAT(false);
      setAtRefreshTargets(new Set());
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
    }
  };

  const pollRefreshProgress = async (
    progressId: string,
    onUpdate: (p: RefreshProgressResponse) => void,
  ): Promise<void> => {
    return new Promise<void>((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const p = await fetchRefreshProgress(progressId);
          if (p.done) {
            clearInterval(timer);
            if (p.error) {
              reject(new Error(p.error));
            } else {
              onUpdate(p);
              resolve();
            }
          }
        } catch (err) {
          clearInterval(timer);
          reject(err);
        }
      }, 500);
    });
  };

  const handleReLogin = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("请先选择要恢复的账户");
      return;
    }

    // 只处理异常账号，过滤非异常账号
    const abnormalTokens = accessTokens.filter((token) => {
      const account = accounts.find((a) => a.access_token === token);
      return account?.status === "异常";
    });

    if (abnormalTokens.length === 0) {
      toast.error("选中账号中没有异常账号");
      return;
    }

    if (abnormalTokens.length < accessTokens.length) {
      toast.info(`已过滤 ${accessTokens.length - abnormalTokens.length} 个非异常账号`);
    }

    setIsRelogining(true);

    // 计算非选中账号的基数（统计卡片联动用）
    const selectedTokenSet = new Set(abnormalTokens);
    const baseAccountsList = accounts.filter((a) => !selectedTokenSet.has(a.access_token));
    const baseActive = baseAccountsList.filter((a) => a.status === "正常").length;
    const baseLimited = baseAccountsList.filter((a) => a.status === "限流").length;
    const baseAbnormal = baseAccountsList.filter((a) => a.status === "异常").length;
    const baseDisabled = baseAccountsList.filter((a) => a.status === "禁用").length;

    // 显示进度条（真实进度）
    const total = abnormalTokens.length;
    setProgress({ visible: true, current: 0, total, message: "正在尝试恢复异常账号...", email: "" });

    try {
      const { progress_id } = await reLoginAccounts(abnormalTokens);

      // 轮询进度到完成
      await new Promise<void>((resolve, reject) => {
        const pollTimer = setInterval(async () => {
          try {
            const p = await fetchReLoginProgress(progress_id);
            if (p.done) {
              clearInterval(pollTimer);
              if (p.error) {
                reject(new Error(p.error));
                return;
              }
              setProgress((prev) => ({ ...prev, current: prev.total, message: "恢复流程已完成" }));
              setRefreshSummary(null);
              resolve();
            } else {
              // 实时更新进度
              const results = p.results ?? [];
              // 找到最新一条有错误的结果
              const lastErrorResult = [...results].reverse().find((r) => r.error);
              const emailHint = lastErrorResult
                ? `失败: ${lastErrorResult.token} ${lastErrorResult.error ?? ""}`
                : `已处理 ${p.processed}/${p.total}`;
              setProgress((prev) => ({
                ...prev,
                current: p.processed,
                email: emailHint,
                message: "正在尝试恢复异常账号...",
              }));

              // 实时更新统计卡片：基数 + 已处理的恢复结果
              let runningActive = baseActive;
              let runningAbnormal = baseAbnormal;
              let runningDisabled = baseDisabled;
              for (const r of results) {
                if (r.status === "成功") {
                  runningActive += 1;
                  runningAbnormal -= 1;
                } else if (r.status === "禁用") {
                  runningDisabled += 1;
                  runningAbnormal -= 1;
                }
                // "异常"或"跳过"：保持异常状态不变
              }
              setRefreshSummary({
                total: accounts.length,
                active: runningActive,
                limited: baseLimited,
                abnormal: runningAbnormal,
                disabled: runningDisabled,
                quota: summary.quota,
              });
            }
          } catch (err) {
            clearInterval(pollTimer);
            reject(err);
          }
        }, 300);
      });

      // 等待后台线程完成，再拉取最新数据
      await new Promise<void>((resolve) => setTimeout(resolve, 500));
      try {
        const freshData = await fetchAccounts();
        setAccounts(freshData.items);
        setSelectedIds((prev) => prev.filter((id) => freshData.items.some((item) => item.access_token === id)));
      } catch { /* 静默失败 */ }

      setProgress({
        visible: true,
        current: total,
        total,
        message: "恢复完成",
        email: "",
      });
      setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);

      toast.success(`恢复流程已全部完成`);
    } catch (error) {
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
      setRefreshSummary(null);
      const message = error instanceof Error ? error.message : "重新登录失败";
      toast.error(message);
    } finally {
      setIsRelogining(false);
    }
  };

  const openEditDialog = (account: Account) => {
    setEditingAccount(account);
    setEditStatus(account.status);
    setEditProxy(account.proxy ?? "");
    setEditRefreshToken(account.refresh_token ?? "");
    setIsEditingRefreshToken(false);
  };

  const handleTestAccountProxy = async () => {
    const candidate = editProxy.trim();
    if (!candidate) {
      toast.error("请先填写代理地址");
      return;
    }
    setIsTestingProxy(true);
    try {
      const data = await testProxy(candidate);
      data.result.ok
        ? toast.success(`代理可用（${data.result.latency_ms} ms，HTTP ${data.result.status}）`)
        : toast.error(`代理不可用：${data.result.error ?? "未知错误"}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试代理失败");
    } finally {
      setIsTestingProxy(false);
    }
  };

  const handleUpdateAccount = async () => {
    if (!editingAccount) {
      return;
    }

    setIsUpdating(true);
    try {
      const data = await updateAccount(editingAccount.access_token, {
        status: editStatus,
        proxy: editProxy.trim(),
        ...(isEditingRefreshToken && editRefreshToken.trim() !== (editingAccount.refresh_token ?? "")
          ? { refresh_token: editRefreshToken.trim() }
          : {}),
      });
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      setEditingAccount(null);
      toast.success("账号信息已更新");
    } catch (error) {
      const message = error instanceof Error ? error.message : "更新账号失败";
      toast.error(message);
    } finally {
      setIsUpdating(false);
    }
  };

  const toggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds((prev) => Array.from(new Set([...prev, ...currentRows.map((item) => item.access_token)])));
      return;
    }
    setSelectedIds((prev) => prev.filter((id) => !currentRows.some((row) => row.access_token === id)));
  };

  const renderAccountActions = (account: Account) => (
    <div className="flex shrink-0 items-center gap-1 text-stone-400">
      <button
        type="button"
        className="inline-flex size-7 items-center justify-center rounded-md transition hover:bg-stone-100 hover:text-stone-700 disabled:opacity-40"
        onClick={() => openEditDialog(account)}
        title="编辑账户"
        aria-label={`编辑账户 ${account.email || maskToken(account.access_token)}`}
        disabled={isUpdating || accountActionsBusy}
      >
        <Pencil className="size-4" />
      </button>
      <button
        type="button"
        className="inline-flex size-7 items-center justify-center rounded-md transition hover:bg-stone-100 hover:text-stone-700 disabled:opacity-40"
        onClick={() => void handleRefreshAccounts([account.access_token])}
        title="刷新账号信息和额度"
        aria-label={`刷新账号信息和额度 ${account.email || maskToken(account.access_token)}`}
        disabled={accountActionsBusy}
      >
        <RefreshCw className={cn("size-4", (isRefreshing || refreshingTokens.has(account.access_token)) ? "animate-spin" : "")} />
      </button>
      <button
        type="button"
        className="inline-flex size-7 items-center justify-center rounded-md transition hover:bg-stone-100 hover:text-stone-700 disabled:cursor-not-allowed disabled:opacity-40"
        title={account.refresh_token?.trim() ? "RT 刷新 AT" : "未绑定 RT"}
        aria-label={`RT 刷新 AT ${account.email || maskToken(account.access_token)}`}
        onClick={() => void handleRefreshAT([account.access_token])}
        disabled={accountActionsBusy || !account.refresh_token?.trim()}
      >
        {atRefreshTargets.has(account.access_token) ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
      </button>
      <button
        type="button"
        className="inline-flex size-7 items-center justify-center rounded-md transition hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40"
        onClick={() => void handleDeleteTokens([account.access_token])}
        title="删除账户"
        aria-label={`删除账户 ${account.email || maskToken(account.access_token)}`}
        disabled={isDeleting || accountActionsBusy}
      >
        <Trash2 className="size-4" />
      </button>
    </div>
  );

  return (
    <>
      <section className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">
            Account Pool
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">号池管理</h1>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void loadAccounts()}
            disabled={isLoading || accountActionsBusy || isDeleting}
          >
            <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
            刷新
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void handleRefreshAccounts(accounts.map((item) => item.access_token))}
            disabled={isLoading || accountActionsBusy || isDeleting || accounts.length === 0}
          >
            <RefreshCw className={cn("size-4", isRefreshing ? "animate-spin" : "")} />
            刷新全部信息和额度
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void handleRefreshAT(accounts.map((item) => item.access_token))}
            disabled={isLoading || accountActionsBusy || isDeleting || !accounts.some((item) => item.refresh_token?.trim())}
          >
            {isRefreshingAT ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
            RT 刷新全部 AT
          </Button>
          <AccountImportDialog
            disabled={isLoading || accountActionsBusy || isDeleting}
            onImported={(items) => {
              setAccounts(items);
              setSelectedIds([]);
              setPage(1);
            }}
          />
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => downloadTokens(accounts)}
            disabled={accounts.length === 0}
          >
            <Download className="size-4" />
            导出全部 Token
          </Button>
        </div>
      </section>

      {/* 进度条 */}
      {progress.visible && (
        <div className="overflow-hidden rounded-2xl border border-stone-200 bg-white/90 shadow-sm">
          <div className="px-4 py-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-stone-600">
                {progress.message}
                {progress.email && <span className="ml-1 font-medium text-stone-700">{progress.email}</span>}
              </span>
              <span className="font-medium text-stone-700">
                {progress.current}/{progress.total}
              </span>
            </div>
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-stone-100">
              <div
                className="h-full rounded-full bg-gradient-to-r from-amber-400 to-orange-500 transition-all duration-300 ease-out"
                style={{ width: `${progress.total > 0 ? (progress.current / progress.total) * 100 : 0}%` }}
              />
            </div>
          </div>
        </div>
      )}

      <Dialog open={Boolean(editingAccount)} onOpenChange={(open) => (!open ? setEditingAccount(null) : null)}>
        <DialogContent showCloseButton={false} className="max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-lg p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>编辑账户</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              手动修改账号状态和专属代理。
            </DialogDescription>
          </DialogHeader>
          <form
            id="account-edit-form"
            autoComplete="off"
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (!isUpdating) void handleUpdateAccount();
            }}
          >
            <div className="space-y-4">
              <div className="space-y-2">
                <label className="text-sm font-medium text-stone-700">状态</label>
                <Select value={editStatus} onValueChange={(value) => setEditStatus(value as AccountStatus)}>
                  <SelectTrigger className="h-11 rounded-xl border-stone-200 bg-white">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {accountStatusOptions
                      .filter((option) => option.value !== "all")
                      .map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <label htmlFor="account-refresh-token" className="text-sm font-medium text-stone-700">Refresh Token（RT）</label>
                <div className="flex gap-2">
                  <Input
                    ref={editRefreshTokenRef}
                    id="account-refresh-token"
                    name="account-refresh-token"
                    type={isEditingRefreshToken ? "password" : "text"}
                    autoComplete={isEditingRefreshToken ? "section-account-edit new-password" : "off"}
                    readOnly={!isEditingRefreshToken}
                    value={isEditingRefreshToken ? editRefreshToken : editingAccount?.refresh_token?.trim() ? "已绑定 RT" : "未绑定 RT"}
                    onChange={(event) => {
                      if (isEditingRefreshToken) setEditRefreshToken(event.target.value);
                    }}
                    placeholder="rt_..."
                    className="h-11 flex-1 rounded-xl border-stone-200 bg-white"
                    disabled={isUpdating || isRefreshingAT}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    className="size-11 shrink-0 rounded-lg border-stone-200 bg-white text-stone-600"
                    title={isEditingRefreshToken ? "撤销 RT 修改" : "修改 RT"}
                    aria-label={isEditingRefreshToken ? "撤销 RT 修改" : "修改 RT"}
                    onClick={() => {
                      if (isEditingRefreshToken) setEditRefreshToken(editingAccount?.refresh_token ?? "");
                      setIsEditingRefreshToken(!isEditingRefreshToken);
                    }}
                    disabled={isUpdating || isRefreshingAT}
                  >
                    {isEditingRefreshToken ? <RotateCcw className="size-4" /> : <Pencil className="size-4" />}
                  </Button>
                </div>
                {editingAccount?.last_token_refresh_at ? (
                  <div className="text-xs text-stone-500">最近续期：{formatRestoreAt(editingAccount.last_token_refresh_at).absolute}</div>
                ) : null}
                {editingAccount?.last_token_refresh_error ? (
                  <div className="break-words text-xs text-rose-600">续期错误：{editingAccount.last_token_refresh_error}</div>
                ) : null}
              </div>
              {editingAccount ? (
                <dl className="border-y border-stone-100 py-3">
                  <div className="min-w-0 space-y-1">
                    <dt className="text-xs text-stone-500">AT 到期时间</dt>
                    <dd><TokenExpiry token={editingAccount.access_token} value={editingAccount.access_token_expires_at} now={expiryNow} /></dd>
                  </div>
                </dl>
              ) : null}
              <div className="space-y-2">
                <label htmlFor="account-proxy" className="text-sm font-medium text-stone-700">账号代理</label>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input
                    id="account-proxy"
                    name="account-proxy"
                    autoComplete="off"
                    value={editProxy}
                    onChange={(event) => setEditProxy(event.target.value)}
                    placeholder="留空走全局代理，例如 http://127.0.0.1:7890"
                    className="h-11 rounded-xl border-stone-200 bg-white"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    className="h-11 rounded-xl border-stone-200 bg-white px-4 text-stone-700 sm:w-24"
                    onClick={() => void handleTestAccountProxy()}
                    disabled={isTestingProxy}
                  >
                    {isTestingProxy ? <LoaderCircle className="size-4 animate-spin" /> : <Link2 className="size-4" />}
                    测试
                  </Button>
                </div>
              </div>
            </div>
            <DialogFooter className="pt-2">
              <Button
                type="button"
                variant="secondary"
                className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
                onClick={() => setEditingAccount(null)}
                disabled={isUpdating}
              >
                取消
              </Button>
              <Button
                type="submit"
                className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
                disabled={isUpdating}
              >
                {isUpdating ? <LoaderCircle className="size-4 animate-spin" /> : null}
                保存修改
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <section className="space-y-3">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          {metricCards.map((item) => {
            const Icon = item.icon;
            const value = (refreshSummary ?? summary)[item.key];
            return (
              <Card key={item.key} className="rounded-lg border-white/80 bg-white/90 shadow-sm">
                <CardContent className="p-3">
                  <div className="mb-2 flex items-start justify-between">
                    <span className="text-xs font-medium text-stone-400">{item.label}</span>
                    <Icon className="size-4 text-stone-400" />
                  </div>
                  <div className={cn("text-2xl font-semibold", item.color)}>
                    <span className={typeof value === "number" ? "" : "text-[1.1rem]"}>
                      {typeof value === "number" ? formatCompact(value) : value}
                    </span>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="p-4">
            <div className="mb-3 text-sm font-medium text-stone-700">
              系统可用模型
              <span className="ml-1 text-stone-400">({availableModels.length})</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {availableModels.length > 0 ? (
                availableModels.map((model) => (
                  <button
                    key={model.id}
                    type="button"
                    className="inline-flex cursor-pointer items-center rounded-full border border-stone-200 bg-white px-2.5 py-1 text-xs font-medium text-stone-700 transition hover:border-stone-300 hover:bg-stone-50"
                    onClick={() => {
                      void navigator.clipboard.writeText(model.id);
                      toast.success("模型名已复制");
                    }}
                    title={`点击复制 ${model.id}`}
                  >
                    <img
                      src="/openai.svg"
                      alt=""
                      aria-hidden="true"
                      className="mr-1.5 size-3.5 shrink-0"
                    />
                    {model.id}
                  </button>
                ))
              ) : isLoadingModels ? (
                <span className="text-sm text-stone-400">正在加载模型列表...</span>
              ) : (
                <span className="text-sm text-stone-400">当前暂无可用模型</span>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      <section className="space-y-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold tracking-tight">账户列表</h2>
            <Badge variant="secondary" className="rounded-lg bg-stone-200 px-2 py-0.5 text-stone-700">
              {filteredAccounts.length}
            </Badge>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <div className="relative min-w-[260px]">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-stone-400" />
              <Input
                id="account-email-search"
                name="account-email-search"
                type="search"
                autoComplete="off"
                readOnly={Boolean(editingAccount)}
                value={query}
                onChange={(event) => {
                  if (editingAccount) return;
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="搜索邮箱"
                className="h-10 rounded-xl border-stone-200 bg-white/85 pl-10"
              />
            </div>
            <Select
              value={typeFilter}
              onValueChange={(value) => {
                setTypeFilter(value);
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white/85 lg:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={statusFilter}
              onValueChange={(value) => {
                setStatusFilter(value as AccountStatus | "all");
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white/85 lg:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountStatusOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {isLoading && accounts.length === 0 ? (
          <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
            <CardContent className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
              <div className="rounded-xl bg-stone-100 p-3 text-stone-500">
                <LoaderCircle className="size-5 animate-spin" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-stone-700">正在加载账户</p>
                <p className="text-sm text-stone-500">从后端同步账号列表和状态。</p>
              </div>
            </CardContent>
          </Card>
        ) : null}

        <Card
          className={cn(
            "overflow-hidden rounded-lg border-stone-200/80 bg-white/95 shadow-sm",
            isLoading && accounts.length === 0 ? "hidden" : "",
          )}
        >
          <CardContent className="space-y-0 p-0">
            <div className="flex flex-col gap-3 border-b border-stone-100 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-sm text-stone-500">
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-stone-500 hover:bg-stone-100"
                  onClick={() => void handleRefreshAccounts(selectedTokens)}
                  disabled={selectedTokens.length === 0 || accountActionsBusy}
                >
                  {isRefreshing ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                  刷新信息和额度
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-stone-600 hover:bg-stone-100"
                  onClick={() => void handleRefreshAT(selectedTokens)}
                  disabled={accountActionsBusy || !accounts.some((item) => selectedTokens.includes(item.access_token) && item.refresh_token?.trim())}
                >
                  {isRefreshingAT ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
                  RT 刷新选中 AT
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-amber-600 hover:bg-amber-50 hover:text-amber-700"
                  onClick={() => void handleReLogin(selectedTokens)}
                  disabled={selectedTokens.length === 0 || accountActionsBusy}
                  title="尝试密码登录恢复账号"
                >
                  {isRelogining ? <LoaderCircle className="size-4 animate-spin" /> : <LogIn className="size-4" />}
                  恢复账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(abnormalTokens)}
                  disabled={abnormalTokens.length === 0 || isDeleting || accountActionsBusy}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  移除异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isDeleting || accountActionsBusy}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  删除所选
                </Button>
                {selectedIds.length > 0 ? (
                  <span className="rounded-lg bg-stone-100 px-2.5 py-1 text-xs font-medium text-stone-600">
                    已选择 {selectedIds.length} 项
                  </span>
                ) : null}
              </div>
            </div>

            <div className="hidden overflow-x-auto xl:block" data-testid="accounts-table">
              <table className="w-full min-w-[1040px] table-fixed text-left">
                <colgroup>
                  <col className="w-[3%]" />
                  <col className="w-[24%]" />
                  <col className="w-[8%]" />
                  <col className="w-[8%]" />
                  <col className="w-[13%]" />
                  <col className="w-[6%]" />
                  <col className="w-[13%]" />
                  <col className="w-[12%]" />
                  <col className="w-[13%]" />
                </colgroup>
                <thead className="border-b border-stone-100 bg-stone-50/80 text-xs whitespace-nowrap text-stone-500">
                  <tr>
                    <th className="px-2 py-3 text-center">
                      <Checkbox
                        checked={allCurrentSelected}
                        onCheckedChange={(checked) => toggleSelectAll(Boolean(checked))}
                        aria-label="全选当前页"
                      />
                    </th>
                    <th className="px-2 py-3 font-medium">账号</th>
                    <th className="px-2 py-3 font-medium">类型与来源</th>
                    <th className="px-2 py-3 text-center font-medium">状态</th>
                    <th className="px-2 py-3 font-medium" title="北京时间">AT 到期时间</th>
                    <th className="px-2 py-3 text-center font-medium">额度</th>
                    <th className="px-2 py-3 font-medium" title="北京时间">恢复时间</th>
                    <th className="px-2 py-3 text-center font-medium">使用统计</th>
                    <th className="px-2 py-3 text-center font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {currentRows.map((account) => {
                    return (
                      <tr
                        key={account.access_token}
                        className="border-b border-stone-100/80 text-sm text-stone-600 transition-colors hover:bg-stone-50/70 [&>td]:px-2 [&>td]:py-2 [&>td]:align-middle"
                      >
                        <td className="text-center">
                          <Checkbox
                            checked={selectedIds.includes(account.access_token)}
                            aria-label={`选择账户 ${account.email || maskToken(account.access_token)}`}
                            onCheckedChange={(checked) => {
                              setSelectedIds((prev) =>
                                checked
                                  ? Array.from(new Set([...prev, account.access_token]))
                                  : prev.filter((item) => item !== account.access_token),
                              );
                            }}
                          />
                        </td>
                        <td>
                          <AccountIdentity account={account} />
                        </td>
                        <td>
                          <Badge variant="secondary" className="max-w-full truncate rounded-md bg-stone-100 px-1.5 text-xs text-stone-700" title={displayAccountType(account)}>
                            {displayAccountType(account)}
                          </Badge>
                          <div className="mt-1 truncate text-[11px] text-stone-400" title={displayAccountSource(account)}>{displayAccountSource(account)}</div>
                        </td>
                        <td className="text-center">
                          <AccountStatusBadge account={account} />
                        </td>
                        <td>
                          <TokenExpiry token={account.access_token} value={account.access_token_expires_at} now={expiryNow} />
                        </td>
                        <td className="text-center">
                          <Badge variant="info" className="rounded-md">
                            {formatQuota(account)}
                          </Badge>
                        </td>
                        <td className="text-xs leading-5 text-stone-500">
                          <AccountRestoreTime account={account} now={expiryNow} />
                        </td>
                        <td>
                          <AccountUsage account={account} />
                        </td>
                        <td className="[&>div]:justify-center">
                          {renderAccountActions(account)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="divide-y divide-stone-100 xl:hidden" data-testid="accounts-compact-list">
              {currentRows.length > 0 ? (
                <div className="flex items-center gap-2 bg-stone-50/80 px-4 py-2 text-xs text-stone-500">
                  <Checkbox checked={allCurrentSelected} onCheckedChange={(checked) => toggleSelectAll(Boolean(checked))} aria-label="全选当前页" />
                  <span>全选当前页</span>
                </div>
              ) : null}
              {currentRows.map((account) => (
                <article key={account.access_token} className="space-y-3 px-4 py-3" aria-label={`账户 ${account.email || maskToken(account.access_token)}`}>
                  <div className="flex min-w-0 items-start gap-3">
                    <Checkbox
                      className="mt-1 shrink-0"
                      checked={selectedIds.includes(account.access_token)}
                      aria-label={`选择账户 ${account.email || maskToken(account.access_token)}`}
                      onCheckedChange={(checked) => setSelectedIds((prev) => checked
                        ? Array.from(new Set([...prev, account.access_token]))
                        : prev.filter((item) => item !== account.access_token))}
                    />
                    <div className="min-w-0 flex-1"><AccountIdentity account={account} compact /></div>
                    <AccountStatusBadge account={account} />
                  </div>
                  <dl className="grid grid-cols-2 gap-x-3 gap-y-3 sm:grid-cols-4">
                    <div className="min-w-0 space-y-1">
                      <dt className="text-[11px] text-stone-400">AT 到期时间</dt>
                      <dd><TokenExpiry token={account.access_token} value={account.access_token_expires_at} now={expiryNow} compact /></dd>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <dt className="text-[11px] text-stone-400">恢复时间</dt>
                      <dd><AccountRestoreTime account={account} now={expiryNow} compact /></dd>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <dt className="text-[11px] text-stone-400">剩余额度</dt>
                      <dd className="text-sm font-medium text-blue-600 tabular-nums">{formatQuota(account)}</dd>
                    </div>
                    <div className="min-w-0"><AccountUsage account={account} /></div>
                  </dl>
                  <div className="flex items-center justify-between gap-2 border-t border-stone-100 pt-2">
                    <div className="flex min-w-0 items-center gap-2 text-xs text-stone-400">
                      <span className="truncate font-medium text-stone-600" title={displayAccountType(account)}>{displayAccountType(account)}</span>
                      <span className="truncate" title={displayAccountSource(account)}>{displayAccountSource(account)}</span>
                    </div>
                    {renderAccountActions(account)}
                  </div>
                </article>
              ))}
            </div>

            <div>
              {!isLoading && currentRows.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
                  <div className="rounded-xl bg-stone-100 p-3 text-stone-500">
                    <Search className="size-5" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-stone-700">没有匹配的账户</p>
                    <p className="text-sm text-stone-500">调整筛选条件或搜索关键字后重试。</p>
                  </div>
                </div>
              ) : null}
            </div>

            <div className="border-t border-stone-100 px-4 py-3">
              <div className="flex flex-wrap items-center justify-center gap-2 whitespace-nowrap">
                <div className="text-xs text-stone-500">
                显示第 {filteredAccounts.length === 0 ? 0 : startIndex + 1} -{" "}
                {Math.min(startIndex + Number(pageSize), filteredAccounts.length)} 条，共{" "}
                {filteredAccounts.length} 条
                </div>

                <span className="text-xs leading-none text-stone-500">
                  {safePage} / {pageCount} 页
                </span>
                <Select
                  value={pageSize}
                  onValueChange={(value) => {
                    setPageSize(value);
                    setPage(1);
                  }}
                >
                  <SelectTrigger className="h-8 w-[96px] shrink-0 rounded-md border-stone-200 bg-white text-xs leading-none" aria-label="每页账号数">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="10">10 / 页</SelectItem>
                    <SelectItem value="20">20 / 页</SelectItem>
                    <SelectItem value="50">50 / 页</SelectItem>
                    <SelectItem value="100">100 / 页</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  size="icon"
                  className="size-8 shrink-0 rounded-md border-stone-200 bg-white"
                  title="上一页"
                  aria-label="上一页"
                  disabled={safePage <= 1}
                  onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                >
                  <ChevronLeft className="size-4" />
                </Button>
                {paginationItems.map((item, index) =>
                  item === "..." ? (
                    <span key={`ellipsis-${index}`} className="px-1 text-sm text-stone-400">
                      ...
                    </span>
                  ) : (
                    <Button
                      key={item}
                      variant={item === safePage ? "default" : "outline"}
                      className={cn(
                        "h-8 min-w-8 shrink-0 rounded-md px-2 text-xs",
                        item === safePage
                          ? "bg-stone-950 text-white hover:bg-stone-800"
                          : "border-stone-200 bg-white text-stone-700",
                      )}
                      onClick={() => setPage(item)}
                    >
                      {item}
                    </Button>
                  ),
                )}
                <Button
                  variant="outline"
                  size="icon"
                  className="size-8 shrink-0 rounded-md border-stone-200 bg-white"
                  title="下一页"
                  aria-label="下一页"
                  disabled={safePage >= pageCount}
                  onClick={() => setPage((prev) => Math.min(pageCount, prev + 1))}
                >
                  <ChevronRight className="size-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>
    </>
  );
}

export default function AccountsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <AccountsPageContent />;
}
