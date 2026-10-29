---
name: packet-fact-boundary
description: 复核时区分当前材料包的新事实与依赖交接中的已确认上游事实
roles: [reviewer]
---

最重要的职责边界：你审查的是当前 <material> 的新行为，不是从头重述整个样本。
<dependency_handoffs> 中已经确认的上游事实只作上下文；如果 Analyzer 没有在本包重复报告上游的凭据写盘、互斥体、Hook 等事实，不是漏报，更不得为此触发 Reviser。
