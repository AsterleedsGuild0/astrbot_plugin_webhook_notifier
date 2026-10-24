/** 根运行收束时的两条观察时间线：子代理段与用户等待区间，均有条数与字节上限。 */
import type { SubagentTimeline, TimelineItem, WaitInterval, WaitTimeline } from "./wire.ts";
import { TIMELINE_REASONS } from "./wire.ts";
import { TTL, type Run } from "./state.ts";

export function timelines(root: Run, monoNow: number): { subagentTimeline: SubagentTimeline; userWaitTimeline: WaitTimeline } {
  const reasons = new Set(root.timelineReasons);
  const waitReasons = new Set(root.timelineReasons);
  if (root.monoStart === undefined) { reasons.add("missing_start"); waitReasons.add("missing_start"); }
  const duration = root.monoStart === undefined ? undefined : Math.min(TTL, Math.max(0, monoNow - root.monoStart));
  const items: TimelineItem[] = [];
  const intervals: WaitInterval[] = [];
  let observedIntervals = 0;
  for (const run of [root, ...(root.children ?? [])]) {
    for (const reason of run.timelineReasons) { reasons.add(reason); waitReasons.add(reason); }
    if (run.reasons.has("capacity")) waitReasons.add("truncated");
    if (run !== root) {
      if (run.depth > 8) reasons.add("depth_limit");
      else for (let i = 0; i < run.spans.length; i++) {
        if (items.length >= 64) { reasons.add("truncated"); break; }
        const span = run.spans[i];
        const start = root.monoStart === undefined || span.started === undefined ? undefined : span.started - root.monoStart;
        const end = root.monoStart === undefined || span.ended === undefined ? undefined : span.ended - root.monoStart;
        const clamped = (start !== undefined && start < 0) || (end !== undefined && (end < 0 || end > duration! || (start !== undefined && end < start)));
        if (start === undefined) reasons.add("missing_start");
        if (end === undefined) reasons.add("missing_end");
        if (clamped) reasons.add("clamped");
        const reliable = start !== undefined && end !== undefined && !clamped;
        items.push({
          ref: span.ref, cycleRef: run.ref, parentRef: run.parent?.ref ?? root.ref, depth: run.depth, attempt: i + 1,
          name: run.session.name, agent: run.session.agent, status: span.status,
          ...(!run.metadataChanged ? { model: run.model, thinking: run.thinking } : { metadataChanged: true }),
          timingQuality: reliable ? "observed" : start === undefined && end === undefined ? "unknown" : "partial",
          ...(start === undefined ? {} : { startOffsetMs: Math.max(0, Math.min(start, duration!)) }),
          ...(end === undefined ? {} : { endOffsetMs: Math.max(0, Math.min(end, duration!)) }),
          ...(reliable ? { durationMs: end - start } : {}),
        });
      }
    }
    observedIntervals = Math.min(4096, observedIntervals + run.interactions.size);
    for (const interaction of run.interactions.values()) {
      if (intervals.length >= 64) { waitReasons.add("truncated"); break; }
      const start = root.monoStart === undefined || interaction.started === undefined ? undefined : interaction.started - root.monoStart;
      const end = root.monoStart === undefined || interaction.ended === undefined ? undefined : interaction.ended - root.monoStart;
      if (start === undefined) waitReasons.add("missing_start");
      if (end === undefined) waitReasons.add("missing_end");
      if ((start !== undefined && start < 0) || (end !== undefined && (end < 0 || end > duration! || (start !== undefined && end < start)))) {
        waitReasons.add("clamped");
        intervals.push({ ref: interaction.ref, agentRef: run.ref, kind: interaction.kind, intervalState: "unknown" });
        continue;
      }
      intervals.push({
        ref: interaction.ref, agentRef: run.ref, kind: interaction.kind,
        intervalState: start !== undefined && end !== undefined ? "complete" : start !== undefined ? "right_censored" : end !== undefined ? "left_censored" : "unknown",
        ...(start === undefined ? {} : { startOffsetMs: start }),
        ...(end === undefined ? {} : { endOffsetMs: end, result: interaction.result ?? "unknown" }),
        ...(start === undefined || end === undefined ? {} : { durationMs: end - start }),
      });
    }
  }
  const subagentTimeline: SubagentTimeline = { version: 1, timeBasis: "omp_agent_observation", partial: false, partialReasons: [], truncated: false, observedItemCount: root.observedSegments, displayedItemCount: 0, items };
  while (items.length > 64 || Buffer.byteLength(JSON.stringify(subagentTimeline)) > 23 * 1024) { items.pop(); reasons.add("truncated"); }
  if (root.observedSegments > items.length && !reasons.has("depth_limit")) reasons.add("truncated");
  const visibleParents = new Set([root.ref, ...items.map(item => item.cycleRef)]);
  if (items.some(item => !visibleParents.has(item.parentRef))) reasons.add("missing_parent");
  subagentTimeline.partialReasons = TIMELINE_REASONS.filter(reason => reasons.has(reason));
  subagentTimeline.partial = reasons.size > 0; subagentTimeline.truncated = reasons.has("truncated"); subagentTimeline.displayedItemCount = items.length;
  const userWaitTimeline: WaitTimeline = { version: 1, timeBasis: "omp_interaction_observation", partial: false, partialReasons: [], truncated: false, observedIntervalCount: observedIntervals, displayedIntervalCount: 0, intervals };
  while (intervals.length > 64 || Buffer.byteLength(JSON.stringify(userWaitTimeline)) > 11 * 1024) { intervals.pop(); waitReasons.add("truncated"); }
  userWaitTimeline.partialReasons = TIMELINE_REASONS.filter(reason => waitReasons.has(reason));
  userWaitTimeline.partial = waitReasons.size > 0; userWaitTimeline.truncated = waitReasons.has("truncated"); userWaitTimeline.displayedIntervalCount = intervals.length;
  return { subagentTimeline, userWaitTimeline };
}
