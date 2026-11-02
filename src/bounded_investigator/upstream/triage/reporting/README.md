# Stage 2 Rich 实时成果面板

当前 reporting 子系统只负责“真实执行事件如何被展示”，不参与 Stage2 判断、重试、second-hop 或最终队列决策。

```text
AgentEvent
├─ EventTraceLogger -> activity.jsonl
└─ Reporter / PacingRenderer
   ├─ AchievementTracker -> 左侧累计成果候选
   ├─ HighlightViewport -> 代表性结论视觉行预算
   ├─ StepLabelResolver -> 结构化阶段 -> 展示步骤名
   ├─ DialogueComposer -> 事实 -> ReportUpdate
   ├─ ReportLifecycle -> task key / #序号 / 时间 / ReportState
   ├─ PhraseChunker -> 自然短语分块
   ├─ PacingRenderer -> 分块节奏 + 完成态提交
   ├─ ReportViewport -> active/history 分组 + 稳定高度槽位
   ├─ RenderScheduler -> 合并脏更新 / 限制整屏刷新频率
   └─ RichRenderer -> alternate-screen 固定双栏绘制
```

## 左侧：第二阶段成果

左侧固定青色边框。代表性结论不再按固定 3 条保存：Tracker 默认保留最近 20 个结构化候选；`HighlightViewport` 按当前左栏宽度折行，默认最多显示 10 个视觉行，单个 `F-xxxx` 最多 2 行，超出部分以省略号截断。显示预算不改变原始 verdict 或成果状态。

## 右侧：实时汇报

- `WORKING`：黄色边框；包含模型请求进行中和结果仍在分块输出的状态。
- `COMPLETE`：绿色边框；最后一个展示块完成后才进入历史区。
- 历史与最新结果位于上方实时汇报区；并发工作状态由底部固定“并行分析”区域展示。
- 标题为 `#序号 · 时间 · 步骤名`。
- 完整结果到达时已知最终文本，Rich 在动画开始前一次性测量并预留最终气泡高度；短语输出期间只填充固定槽位，不重新分页。

## 稳定刷新策略

V14 不再让“一个短语 = 一次整屏刷新”：

1. `PhraseChunker` 仍按自然短语产生小块；
2. `RenderScheduler` 默认最多约 4 FPS 提交 Rich 画面，相邻短语可以合并到同一帧；
3. task OPEN、完整结果到达、COMPLETE、终端尺寸变化等布局边界允许强制刷新；
4. Rich `Live` 使用 `screen=True + auto_refresh=False`，整个工作台运行在固定 alternate screen 中；
5. 终端宽高改变时才重新测量槽位，普通 chunk 不触发高度/分页变化。

所有 pacing / Rich 操作仍在独立展示线程中，不阻塞 Stage2 的 asyncio 模型调用。

V15 presentation boundaries:

- `achievement_panel.py`: fixed-height left-side cards (current status / stage results / directions / highlights).
- `activity_indicator.py`: low-frequency WORKING indicator only; it never changes task state.
- `final_summary_renderer.py`: static Rich final result built directly from the structured payload.
- `RichRenderer`: terminal composition only; `Live.auto_refresh` remains disabled and the activity pulse is ~1 Hz.



## V16：固定并发工作区 + 最新结果

右栏不再把多个 WORKING 完整气泡与历史竞争高度，而是拆成三个展示生命周期：

- `历史消息`：只展示已完成报告，继续按终端剩余高度从旧到新滚动。
- `最新结果`：单独承载 PacingRenderer 当前正在分块展示的最新处理结果；生成期间标记“结果生成中”，完成后保持为最新结果，直到下一条结果到来。
- `当前工作`：根据 `WorkState.max_concurrency` 固定并发槽位。`ActiveTaskSlotAllocator` 只做展示槽位分配，任务结束释放槽位，下一个真实模型请求复用；槽位文本不暴露 `U-xxxx`。

并发槽位来自真实 `attempt_started / attempt_finished`，不会改变 Runner 的 semaphore 或任务顺序。最新结果仍使用预留最终高度和 RenderScheduler 限频，所以短语输出不会推动历史区反复分页。


## V17：底部固定并行分析区 + 精简失败统计

- 右栏使用嵌套固定布局：历史/最新结果位于上方弹性区域，`并行分析`固定锚定底部。
- 最终 Rich 汇总中，Round1/Round2 失败统计全为 0 时显示“无”；非 0 时只显示实际失败类型。
