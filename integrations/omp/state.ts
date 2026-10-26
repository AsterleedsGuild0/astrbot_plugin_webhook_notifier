/** 运行、交互与会话绑定状态：工厂间共享的有界 Map 和父子关联。 */
import type { InteractionDetail, IntervalKind, Reason, Resolution, Session, Signals, Status, TimelineReason } from "./wire.ts";
import type { Assistant, Goal, Speed, Todo, Usage } from "./signals.ts";
import { anonymous } from "./sanitize.ts";

export type Interaction = { ref: string; kind: IntervalKind; open: boolean; started?: number; ended?: number; result?: Resolution; timer?: NodeJS.Timeout; detail?: InteractionDetail };
export type Span = { ref: string; started?: number; ended?: number; status: Status | "running" | "continued" };
export type SnapshotJob = { id: string; agentId?: string; ownerId?: string };
export type Snapshot = { running: SnapshotJob[]; recent: SnapshotJob[] } | null;
export type Run = {
  ref: string; start?: number; monoStart?: number; created: number; session: Session; reasons: Set<Reason>;
  interactions: Map<string, Interaction>; background?: Interaction; spans: Span[]; span?: Span; spanCount: number;
  agentKey?: string; parentKey?: string; depth: number; parent?: Run; root?: Run; children?: Run[];
  initialJobs: Set<string>; observedSegments: number; timelineReasons: Set<TimelineReason>;
  retrying: boolean; retryFailed: boolean; pendingEnd?: Status; stop?: Status; model?: string; thinking?: string;
  metadataChanged: boolean; metadataMissing: boolean; closed: boolean; yielded?: boolean;
  /** 交互式根运行以 aborted 收束时暂缓发送：OMP 计划模式提交计划时先 agent_end(aborted) 再打开评审。 */
  abortTimer?: NodeJS.Timeout; abortAt?: { now: number; mono: number }; planHandoff?: boolean;
  /** 最终 assistant 的结构化结束信息（每次 agent_end 覆盖），用于错误分类。 */
  last?: Assistant;
  usageStart?: Usage; usageEnd?: Usage; context?: Signals["context"];
  /** 会话直接费用（cost − subagentCost）的起止快照与本次助手消息计时。 */
  costStart?: number; costEnd?: number; speed: Speed;
  compactions: number; compactionFailures: number; compactionPending: boolean;
  fallback?: NonNullable<Signals["fallback"]>; todo?: Todo;
};
/** goal 绑定在会话上：创建或更新可能发生在两次运行之间。baseline：子代理 session_start 时的累计快照（首个请求早于 agent_start）。 */
export type Binding = { owner: symbol; key: string; touched: number; run?: Run; snapshot?: () => Snapshot; goal?: Goal; baseline?: unknown };

export const TTL = 86_400_000;
export const MAX_SESSIONS = 128;
export const MAX_INTERACTIONS = 128;
export const bindings = new Map<string, Binding>();
/** 凭据告警的进程内去重：同一 provider 只告警一次。 */
export const alertedProviders = new Set<string>();

export function snapshotJobs(binding: Binding, initial = false): SnapshotJob[] {
  try {
    const snapshot = binding.snapshot?.();
    if (!snapshot) { binding.run?.timelineReasons.add("loading_gap"); return []; }
    if (initial && snapshot.running.length) binding.run?.timelineReasons.add("loading_gap");
    if (snapshot.running.length + snapshot.recent.length > 128) binding.run?.timelineReasons.add("truncated");
    return [...snapshot.running.slice(0, 128), ...snapshot.recent.slice(0, 128)].slice(0, 128);
  } catch { binding.run?.timelineReasons.add("loading_gap"); return []; }
}

/** Factory 共享模块，只关联公开身份与父方快照均能证明的边；从不读取 task 参数或私有 bus。 */
export function linkRuns(): void {
  for (const binding of bindings.values()) {
    const run = binding.run;
    if (!run || run.root || !run.parentKey || run.timelineReasons.has("ambiguous_parent")) continue;
    const parents: Run[] = [];
    for (const candidate of bindings.values()) {
      const parent = candidate.run;
      if (!parent || parent.closed || !parent.root || parent.root.closed || parent.agentKey !== run.parentKey) continue;
      if (snapshotJobs(candidate).some(job => job.agentId && job.ownerId && anonymous(job.agentId) === run.agentKey && anonymous(job.ownerId) === parent.agentKey)) parents.push(parent);
    }
    run.timelineReasons.delete("missing_parent"); run.timelineReasons.delete("ambiguous_parent");
    if (parents.length !== 1) { run.timelineReasons.add(parents.length ? "ambiguous_parent" : "missing_parent"); continue; }
    const parent = parents[0];
    if (run.depth !== parent.depth + 1) { parent.root!.timelineReasons.add("invalid_parent_graph"); continue; }
    run.parent = parent; run.root = parent.root;
    const root = run.root!;
    root.observedSegments = Math.min(4096, root.observedSegments + run.spanCount);
    if (root.children!.length < 64) root.children!.push(run);
    else root.timelineReasons.add("truncated");
    if (run.depth > 8) root.timelineReasons.add("depth_limit");
  }
}
