/**
 * Client for the saas_website JSON API (Odoo `type='json'` / JSON-RPC 2.0).
 *
 * Every backend endpoint returns our envelope `{ ok, data }` or
 * `{ ok, error, code }` inside the JSON-RPC `result`. This module unwraps
 * that and throws a typed `ApiError` so callers can `try/catch`.
 */

export class ApiError extends Error {
  code: string;
  constructor(message: string, code = "error") {
    super(message);
    this.name = "ApiError";
    this.code = code;
  }
}

let rpcId = 0;

// Centralized session-expiry handling (CX-002): AuthContext registers a
// callback here so that ANY request hitting `auth_required` clears the
// session in one place and the route guard redirects to login — components
// must never mask it as a not-found / generic error.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

async function rpc<T = unknown>(
  path: string,
  params: Record<string, unknown> = {}
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "call",
        params,
        id: ++rpcId,
      }),
    });
  } catch {
    throw new ApiError(
      "We couldn't reach the server. Check your connection and try again.",
      "network"
    );
  }

  if (!res.ok) {
    throw new ApiError("Something went wrong. Please try again.", "http_" + res.status);
  }

  const payload = await res.json();

  // JSON-RPC transport-level error (e.g. session expired, server crash).
  if (payload.error) {
    const data = payload.error.data || {};
    const name: string = data.name || "";
    if (name.includes("SessionExpired") || name.includes("AccessDenied")) {
      onUnauthorized?.();
      throw new ApiError("Your session has expired. Please sign in again.", "auth_required");
    }
    throw new ApiError(
      data.message || payload.error.message || "Server error.",
      "server"
    );
  }

  const result = payload.result;
  if (result && typeof result === "object" && "ok" in result) {
    if (!result.ok) {
      const code = result.code || "error";
      // Backend can also signal expiry via the envelope (err(..., 'auth_required')).
      if (code === "auth_required") onUnauthorized?.();
      throw new ApiError(result.error || "Request failed.", code);
    }
    return result.data as T;
  }
  return result as T;
}

/* ────────────────────────────── Types ────────────────────────────── */

export interface ApiUser {
  id: number;
  name: string;
  email: string;
  company: string;
  initials: string;
  phone: string;
  /** True for Odoo internal/backend users (shows a "Backend" menu link). */
  is_internal?: boolean;
}

export interface ApiTier {
  id: number;
  name: string;
  workers: number;
  storage: number;
  monthly: number;
  yearly: number;
  recommended: boolean;
  badge: string;
  sequence: number;
  currency: string;
}

export interface ApiRegion {
  id: number;
  code: string;
  name: string;
  /** Price multiplier applied to the compute+storage portion (1.0 = base). */
  multiplier: number;
  /** Pre-selected default at checkout — now the RECOMMENDED region. */
  default: boolean;
  /** The recommended region (badge "Recommended"). */
  recommended?: boolean;
  /** The cheapest available region (badge "Budget"). */
  budget?: boolean;
  available: boolean;
}

export interface PriceResult {
  workers: number;
  storage: number;
  billing: "monthly" | "yearly";
  total: number;
  monthly_equivalent: number;
  yearly_savings: number;
  savings_percent: number;
  currency: string;
  /** Region multiplier baked into `total` (1.0 = base region). */
  region_factor: number;
  limits: {
    workers: { min: number; max: number };
    storage: { min: number; max: number };
  };
}

export interface PlanConfigMeta {
  // Per-unit rates are intentionally NOT exposed to the client; pricing
  // comes from the calculate endpoint (server-side engine).
  min_workers: number;
  max_workers: number;
  min_storage: number;
  max_storage: number;
  yearly_discount_pct: number;
  currency: string;
  /** Sizing hint: recommended users = workers × [min..max] (light → heavy). */
  users_per_worker_min?: number;
  users_per_worker_max?: number;
}

export interface TrialInfo {
  days: number;
  services_available: boolean;
  hosting_available: boolean;
}

export interface Meta {
  hosting_config: PlanConfigMeta;
  custom_config: PlanConfigMeta;
  trial: TrialInfo;
  sections: { services: boolean; hosting: boolean };
  support_email: string;
  countries: { id: number; name: string; code: string }[];
  domains: { id: number; name: string }[];
  hosting_versions: { id: number; name: string }[];
  support_plans: { code: string; name: string; monthly_price: number; is_default: boolean }[];
  daily_backup_price: number;
}

export interface ApiService {
  id: number;
  name: string;
  tagline: string;
  icon: string;
  is_hosting: boolean;
  image_url: string;
  highlights?: string[];
  description?: string;
  trial_plan_id?: number;
  features?: { title: string; description: string }[];
  plans?: {
    id: number;
    name: string;
    workers: number;
    storage_gb: number;
    is_trial: boolean;
  }[];
}

export type InstanceState =
  | "draft"
  | "pending_payment"
  | "paid"
  | "pending_provision"
  | "provisioning"
  | "running"
  | "stopped"
  | "suspended"
  | "failed"
  | "cancelled"
  | "cancelled_by_client";

export interface ApiUsage {
  cpu: number;
  ram: number;
  storage: number;
}

export interface ApiBackup {
  id: number;
  label: string;
  type: "manual" | "automatic";
  size_mb: number;
  created: string;
  status: "available" | "in_progress" | "failed";
  download_url: string;
  is_full_instance: boolean;
  db_name?: string;
  format?: string;
}

export interface ApiInvoice {
  id: number;
  number: string;
  status: "paid" | "open" | "overdue" | "draft";
  issued: string;
  due: string;
  total: number;
  residual: number;
  currency: string;
  currency_symbol: string;
  instance_name: string;
  portal_url: string;
  payable: boolean;
  lines?: { description: string; quantity: number; total: number }[];
  subtotal?: number;
  tax?: number;
}

export interface ApiPaymentMethod {
  id: number;
  provider: string;
  label: string;
  is_default: boolean;
}

/** v47 capacity ("upgrade experience") — positive framing, never overage. */
export interface CapacitySummary {
  /** internal state: ok | warn80 | full | grace | restricted */
  state: string;
  /** plain-language headline + body the UI shows verbatim */
  title: string;
  message: string;
  tone: "neutral" | "info" | "warning" | "paused";
  used_gb: number;
  capacity_gb: number;
  usage_pct: number;
  /** purchased storage blocks + the block size/price for the "Add storage" CTA */
  blocks_owned: number;
  block_gb: number;
  block_price: number;
  /** grace countdown when at capacity */
  grace_days_left: number | null;
  currency: string;
}

export interface WalletInline {
  /** customer's own money — never expires */
  funded: number;
  /** bonus/system credit — may expire */
  bonus: number;
  total: number;
  /** soonest bonus expiry date, if any bonus credit exists */
  bonus_expiry: string | null;
  currency: string;
}

export interface WalletTransaction {
  id: number;
  date: string;
  amount: number;
  balance_after: number;
  kind: string;
  credit_class: "customer_funded" | "system_issued" | false;
  description: string;
}

export interface WalletData {
  /** customer's own money — never expires */
  balance_funded: number;
  /** bonus/system credit — may expire */
  balance_bonus: number;
  balance: number;
  currency: string;
  bonus_expiry: string | null;
  transactions: WalletTransaction[];
}

/** Odoo.sh-style environment: Production or a Staging/Development server. */
export type EnvironmentType = "production" | "staging" | "development";

export interface EnvChild {
  id: number;
  name: string;
  domain: string;
  url: string;
  version: string;
  environment: EnvironmentType;
  environment_label: string;
  branch: string;
  state: InstanceState;
  state_label: string;
  access_token: string;
  is_production: boolean;
  pending_payment: boolean;
  pending_invoice_id: number | false;
}

export interface ProjectEnvironments {
  project_id: number;
  project_name: string;
  production: EnvChild;
  main_branch: string;
  env_server_price: number;
  billing_cycle: "monthly" | "yearly";
  /** Env servers require a Git repo connected to Production. */
  has_repo: boolean;
  repo_url: string;
  environments: EnvChild[];
  /** Production server's current resources — for the "Scale resources" CTA. */
  production_plan: {
    plan_name: string;
    workers: number;
    storage_gb: number;
    is_trial: boolean;
  };
  /** Purchased vs in-use Staging/Development slots. */
  slots: {
    staging: { used: number; total: number };
    development: { used: number; total: number };
  };
}

export interface ProjectPriceResult extends PriceResult {
  env_server_price: number;
  staging_count: number;
  dev_count: number;
  env_total: number;
  project_total: number;
}

export interface ApiInstance {
  id: number;
  name: string;
  domain: string;
  url: string;
  region: string;
  version: string;
  state: InstanceState;
  state_label: string;
  workers: number;
  storage_gb: number;
  billing_cycle: "monthly" | "yearly";
  created: string;
  is_hosting: boolean;
  is_trial: boolean;
  usage: ApiUsage;
  access_token: string;
  // environments
  environment: EnvironmentType;
  environment_label: string;
  branch: string;
  parent_id: number | false;
  main_branch?: string;
  env_server_price?: number;
  environments?: EnvChild[];
  // detail-only
  plan_name?: string;
  next_invoice_date?: string;
  daily_backup_enabled?: boolean;
  daily_backup_suspended?: boolean;
  daily_backup_pending?: boolean;
  daily_backup_price?: number;
  daily_backup_next_invoice_date?: string;
  pip_packages?: string;
  pip_install_error?: string;
  last_error?: string;
  repo?: { url: string; branch: string; has_token: boolean; state: string };
  pending_plan?: string;
  scheduled_plan?: string;
  backups?: ApiBackup[];
  invoices?: ApiInvoice[];
  has_unpaid_invoice?: boolean;
  checkout_url?: string;
  // optional unpaid invoice the customer may decline (0/false if none)
  cancellable_invoice_id?: number | false;
  // cancelled-instance reactivation (detail-only)
  is_cancelled?: boolean;
  has_retained_snapshot?: boolean;
  retained_snapshot_date?: string;
  restoration_fee?: number;
  currency?: string;
  reactivate_url?: string;
  // billing (detail-only)
  auto_renew_subscription?: boolean;
  auto_renew_daily_backup?: boolean;
  payment_method?: ApiPaymentMethod | null;
  capacity?: CapacitySummary;
  wallet?: WalletInline;
}

export interface DashboardData {
  instances: ApiInstance[];
  recent_invoices: ApiInvoice[];
  wallet?: WalletInline;
  currency?: string;
  stats: {
    instances: number;
    running: number;
    open_invoices: number;
    outstanding: number;
    wallet_balance?: number;
  };
}

export interface StatusData {
  id: number;
  state: InstanceState;
  state_label: string;
  url: string;
  usage?: ApiUsage;
  backup_running: boolean;
  db_ops_running: boolean;
  provisioning_log?: string;
}

export interface MetricSample {
  t: string;
  cpu: number;
  ram: number;
  storage_mb: number;
  storage_pct: number;
}

export interface MetricsHistory {
  instance: string;
  hours: number;
  bucket_seconds: number;
  retention_days: number;
  plan: { cpu_limit: number; ram_limit: string; storage_limit_gb: number };
  samples: MetricSample[];
}

export interface Build {
  id: number;
  source: "initial" | "push" | "redeploy" | "merge";
  state: "running" | "success" | "failed";
  branch: string;
  commit: string;
  commit_message: string;
  author: string;
  at: string;
  duration_s: number | null;
  log: string;
  commit_url: string;
}

export interface DbOperationStatus {
  id: number;
  operation: "create" | "duplicate" | "drop" | "upgrade";
  db_name: string;
  state: "running" | "done" | "failed";
  error: string;
  output: string;
}

export interface DbListData {
  databases: { name: string; login: string }[];
  ready: boolean;
  state?: InstanceState;
  /** Instance host, e.g. https://acme.veltnex.com. A specific DB is
   *  opened at `${url}/web?db=${name}` (all DBs share the host). */
  url?: string;
  pending_ops?: { db_name: string; operation: string }[];
}

export interface SqlResult {
  columns: string[];
  /** Row cells: string | number | boolean | null (typed from Postgres). */
  rows: (string | number | boolean | null)[][];
  rowcount: number;
  truncated: boolean;
  error: string | null;
}

/* ──────────────────────────── Endpoints ──────────────────────────── */

export const api = {
  // session / account
  me: () => rpc<ApiUser>("/saas/api/v1/me"),
  login: (login: string, password: string) =>
    rpc<ApiUser>("/saas/api/v1/auth/login", { login, password }),
  logout: () => rpc("/saas/api/v1/auth/logout"),
  registerStart: (form: Record<string, unknown>) =>
    rpc<{ otp_sent: boolean }>("/saas/api/v1/auth/register/start", form),
  registerResend: (phone: string) =>
    rpc<{ otp_sent: boolean }>("/saas/api/v1/auth/register/resend", { phone }),
  registerVerify: (form: Record<string, unknown>) =>
    rpc<ApiUser>("/saas/api/v1/auth/register/verify", form),
  // Password reset (in-SPA, email OTP). `start` is account-enumeration safe.
  resetStart: (email: string) =>
    rpc<{ sent: boolean }>("/saas/api/v1/auth/reset/start", { email }),
  resetVerify: (email: string, otp: string, password: string) =>
    rpc<ApiUser>("/saas/api/v1/auth/reset/verify", { email, otp, password }),

  // public
  meta: () => rpc<Meta>("/saas/api/v1/meta"),
  tiers: (kind = "hosting", region?: number | null) =>
    rpc<ApiTier[]>("/saas/api/v1/tiers", { kind, region: region ?? undefined }),
  regions: () => rpc<ApiRegion[]>("/saas/api/v1/regions"),
  services: () => rpc<ApiService[]>("/saas/api/v1/services"),
  service: (id: number) => rpc<ApiService>(`/saas/api/v1/services/${id}`),
  hostingCalculate: (
    workers: number,
    storage: number,
    billing: string,
    region?: number | null,
  ) =>
    rpc<PriceResult>("/saas/api/v1/hosting/calculate", {
      workers,
      storage,
      billing,
      region: region ?? undefined,
    }),
  servicesCalculate: (
    workers: number,
    storage: number,
    billing: string,
    region?: number | null,
  ) =>
    rpc<PriceResult>("/saas/api/v1/services/calculate", {
      workers,
      storage,
      billing,
      region: region ?? undefined,
    }),
  checkSubdomain: (subdomain: string, domain_id: number) =>
    rpc<{ available: boolean; message: string }>("/saas/api/v1/check-subdomain", {
      subdomain,
      domain_id,
    }),

  // portal — billing (A1 + A4)
  wallet: () => rpc<WalletData>("/saas/api/v1/wallet"),
  paymentMethods: () =>
    rpc<ApiPaymentMethod[]>("/saas/api/v1/billing/payment-methods"),
  removePaymentMethod: (methodId: number) =>
    rpc<{ removed: boolean }>(
      `/saas/api/v1/billing/payment-methods/${methodId}/remove`,
    ),
  setAutoRenew: (
    instanceId: number,
    opts: { subscription?: boolean; daily_backup?: boolean },
  ) =>
    rpc<{ auto_renew_subscription: boolean; auto_renew_daily_backup: boolean }>(
      `/saas/api/v1/instances/${instanceId}/auto-renew`,
      opts,
    ),
  addStorageBlock: (instanceId: number, qty = 1) =>
    rpc<{ invoice_id?: number; checkout_url?: string; amount?: number; activated?: boolean }>(
      `/saas/api/v1/instances/${instanceId}/storage/add`,
      { qty },
    ),
  releaseStorageBlock: (instanceId: number, qty = 1) =>
    rpc<{ released: boolean; blocks_owned: number }>(
      `/saas/api/v1/instances/${instanceId}/storage/release`,
      { qty },
    ),

  // portal
  dashboard: () => rpc<DashboardData>("/saas/api/v1/dashboard"),
  instances: (itype?: string) =>
    rpc<ApiInstance[]>("/saas/api/v1/instances", itype ? { itype } : {}),
  instance: (id: number, accessToken?: string) =>
    rpc<ApiInstance>(`/saas/api/v1/instances/${id}`, accessToken ? { access_token: accessToken } : {}),
  instanceStatus: (id: number) => rpc<StatusData>(`/saas/api/v1/instances/${id}/status`),
  instanceMetrics: (id: number, accessToken?: string) =>
    rpc<{ cpu: number; ram: number; at: string }>(
      `/saas/api/v1/instances/${id}/metrics`,
      accessToken ? { access_token: accessToken } : {},
    ),
  instanceMetricsHistory: (id: number, range: string, accessToken?: string) =>
    rpc<MetricsHistory>(
      `/saas/api/v1/instances/${id}/metrics/history`,
      { range, ...(accessToken ? { access_token: accessToken } : {}) },
    ),
  instanceBuilds: (id: number, accessToken?: string) =>
    rpc<Build[]>(
      `/saas/api/v1/instances/${id}/builds`,
      accessToken ? { access_token: accessToken } : {},
    ),
  instanceAction: (id: number, action: string) =>
    rpc<StatusData>(`/saas/api/v1/instances/${id}/action`, { action }),

  databases: (id: number) => rpc<DbListData>(`/saas/api/v1/instances/${id}/databases`),
  sqlQuery: (id: number, db: string, query: string, limit = 1000) =>
    rpc<SqlResult>(`/saas/api/v1/instances/${id}/sql`, { db, query, limit }),

  /** Place a hosting order from the SPA (no Review page). Returns where to
   *  send the buyer next (payment checkout, or the instance for a trial). */
  hostingOrder: (fields: Record<string, string>) =>
    rpc<{ redirect_url: string }>(`/saas/api/v1/hosting/order`, fields),

  // Odoo.sh-style in-browser shell (terminal) into the instance container.
  // Output streams over SSE at terminalOutputUrl(); input/resize/close are RPC.
  terminalCreate: (id: number, cols: number, rows: number) =>
    rpc<{ session_id: string; initial_output: string }>(
      `/saas/terminal/instance/create`,
      { instance_id: id, cols, rows },
    ),
  terminalInput: (sid: string, data: string) =>
    rpc(`/saas/terminal/instance/input`, { session_id: sid, data }),
  terminalResize: (sid: string, cols: number, rows: number) =>
    rpc(`/saas/terminal/instance/resize`, { session_id: sid, cols, rows }),
  terminalClose: (sid: string) =>
    rpc(`/saas/terminal/instance/close`, { session_id: sid }),
  dbCreate: (id: number, name: string, login: string, password: string) =>
    rpc<{ db_name: string }>(`/saas/api/v1/instances/${id}/databases/create`, {
      name,
      login,
      password,
    }),
  dbDrop: (id: number, name: string) =>
    rpc<{ db_name: string }>(`/saas/api/v1/instances/${id}/databases/drop`, { name }),
  dbDuplicate: (id: number, source: string, name: string) =>
    rpc<{ db_name: string }>(`/saas/api/v1/instances/${id}/databases/duplicate`, {
      source,
      name,
    }),
  dbRestoreUploadUrl: (id: number, name: string) =>
    rpc<{ backup_id: number; upload_url: string; db_name: string }>(
      `/saas/api/v1/instances/${id}/databases/restore/upload-url`,
      { name },
    ),
  dbRestoreStart: (id: number, backupId: number) =>
    rpc(`/saas/api/v1/instances/${id}/databases/restore/start`, {
      backup_id: backupId,
    }),
  dbUpgrade: (id: number, name: string, modules: string) =>
    rpc<{ db_name: string; op_id: number }>(
      `/saas/api/v1/instances/${id}/databases/upgrade`,
      { name, modules },
    ),
  dbOperation: (id: number, opId: number) =>
    rpc<DbOperationStatus>(`/saas/api/v1/instances/${id}/databases/operation/${opId}`),
  dbBackup: (id: number, name: string, format: "zip" | "dump" = "zip") =>
    rpc<{ backup_id: number }>(`/saas/api/v1/instances/${id}/databases/backup`, { name, format }),
  dailyBackupEnable: (id: number) =>
    rpc<{ checkout_url: string }>(`/saas/api/v1/instances/${id}/daily-backup/enable`),
  setRepo: (
    id: number,
    p: { repo_url: string; repo_branch: string; git_token?: string }
  ) => rpc(`/saas/api/v1/instances/${id}/repo`, p),
  setPackages: (id: number, pip_packages: string) =>
    rpc(`/saas/api/v1/instances/${id}/packages`, { pip_packages }),
  invoiceCancel: (id: number) =>
    rpc<{ result: string; state: string }>(`/saas/api/v1/instances/${id}/invoice/cancel`),
  dbResetPassword: (id: number, name: string, new_password: string, login?: string) =>
    rpc<{ login: string }>(`/saas/api/v1/instances/${id}/databases/reset-password`, {
      name,
      new_password,
      login: login || undefined,
    }),

  backups: (id: number) =>
    // The endpoint returns {backups, ready, state}; when the instance is
    // stopped/suspended/not-running, ready=false and backups=[].
    rpc<{ backups: ApiBackup[]; ready: boolean; state: string }>(
      `/saas/api/v1/instances/${id}/backups`,
    ),
  backupRestore: (id: number, backupId: number, confirm: string) =>
    rpc<{ state: string }>(`/saas/api/v1/instances/${id}/backups/${backupId}/restore`, { confirm }),
  backupCreate: (id: number) => rpc(`/saas/api/v1/instances/${id}/backups/create`),

  invoices: () => rpc<ApiInvoice[]>("/saas/api/v1/invoices"),
  invoice: (id: number) => rpc<ApiInvoice>(`/saas/api/v1/invoices/${id}`),

  // portal — Odoo.sh-style environments
  environments: (id: number) =>
    rpc<ProjectEnvironments>(`/saas/api/v1/instances/${id}/environments`),
  environmentCreate: (
    id: number,
    type: "staging" | "development",
    name?: string,
    branch?: string,
  ) =>
    rpc<{
      child_id: number;
      auto_provisioned: boolean;
      invoice_id?: number;
      checkout_url?: string;
    }>(`/saas/api/v1/instances/${id}/environments/create`, {
      type,
      name: name || undefined,
      branch: branch || undefined,
    }),
  environmentReserve: (
    id: number,
    type: "staging" | "development",
    qty = 1,
  ) =>
    rpc<{
      auto_provisioned: boolean;
      reserved?: number;
      invoice_id?: number;
      checkout_url?: string;
    }>(`/saas/api/v1/instances/${id}/environments/reserve`, { type, qty }),
  environmentRelease: (
    id: number,
    type: "staging" | "development",
    qty = 1,
  ) =>
    rpc<{ released: number; slots: number }>(
      `/saas/api/v1/instances/${id}/environments/release`,
      { type, qty },
    ),
  environmentDelete: (id: number, childId: number, deleteBranch: boolean) =>
    rpc<{ deleted: boolean }>(
      `/saas/api/v1/instances/${id}/environments/${childId}/delete`,
      { delete_branch: deleteBranch },
    ),
  environmentMerge: (id: number, sourceId: number, targetId: number) =>
    rpc<{
      status: "merged" | "up_to_date";
      source_branch: string;
      target_branch: string;
      redeployed: boolean;
    }>(`/saas/api/v1/instances/${id}/environments/merge`, {
      source_id: sourceId,
      target_id: targetId,
    }),
  instanceBranches: (id: number) =>
    rpc<{ branches: string[]; main_branch: string }>(
      `/saas/api/v1/instances/${id}/branches`,
    ),
  hostingCalculateProject: (p: {
    workers: number;
    storage: number;
    billing: string;
    region?: number | null;
    staging_count: number;
    dev_count: number;
  }) =>
    rpc<ProjectPriceResult>("/saas/api/v1/hosting/calculate-project", {
      ...p,
      region: p.region ?? undefined,
    }),
};

/**
 * Upload a file straight to the bucket using a presigned PUT URL.
 * Bypasses Odoo entirely (no worker held, no request timeout), so it
 * scales to large backup files. Reports progress 0..1 via onProgress.
 *
 * Note: the bucket must allow cross-origin PUT from this app's origin
 * (a one-time CORS rule), and the Content-Type must match what the
 * presigned URL was signed with (application/zip).
 */
export function uploadToBucket(
  url: string,
  file: File | Blob,
  onProgress?: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);
    xhr.setRequestHeader("Content-Type", "application/zip");
    xhr.upload.onprogress = (e) => {
      if (onProgress && e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new ApiError("The upload was rejected by storage.", "upload_failed"));
    };
    xhr.onerror = () =>
      reject(new ApiError("The upload failed. Check your connection and try again.", "upload_failed"));
    xhr.onabort = () => reject(new ApiError("Upload cancelled.", "upload_cancelled"));
    xhr.send(file);
  });
}

/** SSE URL for an instance's live container logs (served by saas_core). */
export function logStreamUrl(instanceId: number, tail = 100) {
  return `/saas/instance/${instanceId}/logs/stream?tail=${tail}`;
}

/** SSE endpoint that streams a shell session's output (base64 chunks). */
export function terminalOutputUrl(sessionId: string) {
  return `/saas/terminal/instance/output/${sessionId}`;
}

