# DeepExtract 样本恶意代码分析报告

**优化交付版**

| **项目** | **内容**                                                         |
|----------|------------------------------------------------------------------|
| 样本标识 | 07b8                                                             |
| MD5      | df8a2c219c032bef70fe59d8d2ef393a                                 |
| SHA1     | 2440b3ec995ace60d31dcfbd9e4f011baffad4ff（外部精确哈希记录补充） |
| SHA256   | 07b8f25e7b536f5b6f686c12d04edc37e11347c8acd5c53f98a174723078c365 |
| 分析方式 | DeepExtract 静态导出物与 Skill 产物复核；未执行样本              |
| 风险等级 | 严重（Critical）                                                 |
| 最终定性 | 确认恶意；配置驱动的凭据窃取、持久化、输入阻断和进程注入组件     |
| 家族归因 | 高置信度与 CyberGate / Rebhip 代码系一致；归因与静态事实分离     |
| 报告日期 | 2026-07-13                                                       |

**核心行为链**

**配置解码 → 模式分派 → 反分析 → 安装/持久化 → 凭据窃取 → 固定文件落盘 → PE 注入 → 自删除**

## 目录

1. 执行摘要
2. 结论与影响
3. 分析范围与证据质量
4. 核心恶意行为链
5. 凭据收集能力
6. 配置与关键常量
7. 安装、持久化与清理
8. 反分析与输入阻断
9. IOC
10. MITRE ATT&CK 映射
11. 检测建议
12. 应急处置建议
13. 家族归因
14. 未决缺口与边界
15. 关键函数证据索引
附录 A：YARA 静态筛查规则
附录 B：最终判断

## 1. 执行摘要

静态代码确认，该样本以导出函数 start 作为唯一主入口，通过固定互斥体选择三类执行模式：凭据收集模式、键鼠输入阻断模式、常规安装与注入模式。常规路径从 PE 的 RCDATA 资源 XX-XX-XX-XX 读取配置，按 ####@#### 分割，对字段逐字节 XOR 0xBC，形成固定 101 字段配置。配置控制安装目录、文件名、持久化方式、目标进程及反分析开关。

样本具备多来源凭据收集能力，覆盖 No-IP DUC、Windows Live/MSN、旧版 Firefox NSS、RAS 拨号、IE Protected Storage、IE IntelliForms/Storage2 以及 Windows Credential Manager。非空结果会写入 %TEMP% 下固定文件。常规路径可复制自身、写入 Run/Policies Run/Active Setup 持久化，并将配置携带的 PE 形态数据手工映射到 explorer.exe 或配置指定宿主。另有一条路径把当前模块复制进 Shell 进程，远程循环删除原文件。

当前导出物没有提供资源正文和内层 PE 字节，因此无法恢复实际 C2、端口、安装路径、安装文件名、配置互斥体和内层载荷行为。外层已审代码也未呈现明确网络客户端调用，不能把其他 CyberGate 样本的域名、IP 或端口当作本样本 IOC。

**风险判断：该模块直接访问多类本地凭据存储，并具备持久化和合法进程注入能力，应按严重级别处置。**

## 2. 结论与影响

| **项目**  | **结论**                                                           | **置信度**     |
|-----------|--------------------------------------------------------------------|----------------|
| 恶意性    | 确认恶意                                                           | 高             |
| 核心目的  | 凭据收集、持久化、代码注入、反分析和文件清理                       | 高             |
| 输入 Hook | 用于阻断键盘和鼠标事件；未确认按键记录或外传                       | 高             |
| 网络通信  | 外层静态代码未确认 C2 或网络客户端                                 | 高             |
| 内层 PE   | 确认存在映射执行机制；具体功能未知                                 | 高             |
| 家族      | 与 CyberGate / Rebhip 代码系高度一致                               | 高（外部情报） |
| 主要风险  | 浏览器、系统凭据、RAS 和第三方账户材料可能泄露；合法进程可能被注入 | 高             |

## 3. 分析范围与证据质量

- DeepExtract 原始函数清单为 500 个函数，Skill ledger 对 500/500 函数完成状态覆盖。

- 覆盖校验 23/23 通过，报告状态为 READY_FOR_REPORT_WITH_GAPS。

- 本报告只使用静态导出、函数代码、调用关系、字符串、API 和 Skill 账本；未执行、释放或联网分析样本。

- 结论分为 Confirmed（静态闭环）、External attribution（外部归因）和 Gap（当前输入无法回答）。

## 4. 核心恶意行为链

| **阶段**         | **行为**                                                       | **结论与证据**                                |
|------------------|----------------------------------------------------------------|-----------------------------------------------|
| 1\. 配置加载     | 读取 RCDATA 资源，分割并 XOR 0xBC，填充 101 字段配置           | Confirmed；F309/F310/F307/F325                |
| 2\. 模式分派     | 固定互斥体控制更新等待、凭据收集、输入阻断和常规执行           | Confirmed；F500 L0042-L0195                   |
| 3\. 反分析       | 按配置执行沙箱、虚拟机、调试器、环境与时间检查                 | Confirmed（开关值未知）；F494                 |
| 4\. 安装与持久化 | 选择目录、复制自身，写入 Run、Policies Run、Active Setup       | Confirmed（路径/值名未知）；F394/F388         |
| 5\. 凭据收集     | 收集 No-IP、Windows Live、Firefox、RAS、IE、Credential Manager | Confirmed；F480/F482/F483/F440/F419/F464/F465 |
| 6\. 结果落盘     | GetTempPathA 生成临时目录，仅在结果非空时写固定文件            | Confirmed；F370/F373/F367/F500                |
| 7\. PE 注入      | 构造映像、重定位、修复导入、写远程进程并调用入口               | Confirmed；F302/F299/F301/F303/F304           |
| 8\. 自删除       | 把当前模块复制到 Shell 进程，远程循环删除当前文件              | Confirmed；F488/F489/F486                     |

### 4.1 执行模式

1. `_x_X_UPDATE_X_x_` 已存在：等待约 12 秒后继续。

2. `_x_X_PASSWORDLIST_X_x_` 已存在：进入凭据收集模式，生成多类凭据输出文件。

3. `_x_X_BLOCKMOUSE_X_x_` 已存在：安装低级键盘与鼠标 Hook，阻断输入。

4. 其他情况：加载配置、反分析、单实例控制、安装与持久化、配置写回、持续注入和常规注入。

## 5. 凭据收集能力

| **数据源**           | **访问位置/API**                      | **提取内容**              | **输出文件**           | **证据**        |
|----------------------|---------------------------------------|---------------------------|------------------------|-----------------|
| No-IP DUC            | HKLM\SOFTWARE\Vitalwerks\DUC          | UserName、Base64 Password | %TEMP%\NOIP.abc        | F480；F470/F471 |
| Windows Live/MSN     | CredEnumerateA("WindowsLive:name=\*") | 用户名、CredentialBlob    | MSN.abc；xxxyyyzzz.dat | F482            |
| Firefox（旧版）      | profiles.ini、signons\*.txt、nss3.dll | NSS 解密后的登录材料      | FIREFOX.abc            | F483            |
| RAS 拨号             | RAS API、DialParamsUID、LSA 私有数据  | 拨号项、用户名、密码      | IELOGIN.abc            | F440            |
| IE Protected Storage | PStoreCreateInstance / :StringData    | 旧版 IE 保存数据          | IEPASS.abc             | F419            |
| IE Storage2          | IntelliForms\Storage2 + DPAPI         | 自动完成记录              | IEAUTO.abc             | F461/F464       |
| Credential Manager   | CredEnumerateA + DPAPI                | Target、User、Password    | IEWEB.abc              | F465            |

*说明：固定输出文件是高价值 IOC；正常软件注册表路径本身不是感染证据，应与异常进程、输出文件、持久化和注入事件组合判断。*

## 6. 配置与关键常量

| **项目**      | **已确认内容**                                        | **状态**             |
|---------------|-------------------------------------------------------|----------------------|
| 资源名称/类型 | XX-XX-XX-XX / RCDATA（10）                            | Confirmed            |
| 字段数量      | 101                                                   | Confirmed            |
| 分隔符        | ####@####                                            | Confirmed            |
| 字段变换      | 逐字节 XOR 0xBC；输入输出等长                         | Confirmed            |
| 布尔解析      | 仅精确等于 TRUE 时为真                                | Confirmed            |
| 配置写回      | 101 字段重新编码并序列化；XX--XX--XX.txt 参与输出路径 | Confirmed；内容未知  |
| 安装目录模式  | 系统、Windows、用户或自定义目录                       | 结构确认；实际值未知 |
| 注入宿主      | explorer.exe 或配置指定程序；可暂停创建后注入         | 结构确认；实际值未知 |
| 单实例互斥体  | 配置名称及 `<配置名称>_PERSIST`                      | 结构确认；实际值未知 |
| C2/端口       | 未恢复                                                | Gap                  |
| 内层 PE       | 字节未提供                                            | Gap                  |

### 6.1 密码学与解码

- 配置：固定 XOR 0xBC，属于轻量混淆，不是强加密。

- No-IP：标准 Base64 解码特征，包括四字符分组和 = 填充处理。

- IE Storage2：对 URL 的 UTF-16（含 NUL）做 SHA-1，生成注册表值名。

- Firefox：调用 NSS 的 NSSBase64_DecodeBuffer 和 PK11SDR_Decrypt。

- IE/Windows 凭据：调用 CryptUnprotectData；静态代码只能证明尝试解密，不能证明运行时成功。

## 7. 安装、持久化与清理

### 7.1 注册表持久化

| **Hive** | **路径**                                                        | **数据**                      |
|----------|-----------------------------------------------------------------|-------------------------------|
| HKLM     | Software\Microsoft\Windows\CurrentVersion\Run                   | 配置值名 → 安装文件路径       |
| HKCU     | Software\Microsoft\Windows\CurrentVersion\Run                   | 配置值名 → 安装文件路径       |
| HKLM     | Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run | 配置值名 → 安装文件路径       |
| HKCU     | Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run | 配置值名 → 安装文件路径       |
| HKLM     | `Software\Microsoft\Active Setup\Installed Components\<配置 id>` | `StubPath=<安装路径> Restart` |

静态外层代码未发现服务创建或计划任务实现。该结论不延伸到不可见的内层 PE。

### 7.2 进程注入

- 目标可为现有 explorer.exe、配置指定的现有进程，或以 CREATE_SUSPENDED 创建的宿主。

- 映射器复制 PE 头与节，处理基址重定位，按名称或序号修复导入，并依据节标志调整保护。

- 远程路径使用 VirtualAllocEx、WriteProcessMemory 和远程线程执行入口，符合 PE Injection。

- 上述证据证明代码具备注入设计，不代表当前运行环境中一定成功。

### 7.3 自删除

样本定位 Shell 窗口对应进程，把当前模块映像和目标路径复制到远程进程，启动远程删除函数；DeleteFileA 失败时每 100 ms 重试。该路径用于在原进程退出后清除当前文件。

## 8. 反分析与输入阻断

- Sandboxie 模块，例如 SbieDll.dll。

- VirtualBox 相关进程，例如 VBoxService.exe。

- 调试器和分析模块，例如 IsDebuggerPresent、PEB、dbghelp.dll。

- SoftICE/NTICE 设备，例如 \\.\SICE、\\.\NTICE。

- ProductId、用户名、进程/模块和时间阈值等环境特征。

输入阻断模式安装键盘与鼠标低级 Hook。可见回调没有形成按键保存、文件输出或网络外传闭环，因此本报告只认定为键鼠阻断，不认定为已确认键盘记录。

## 9. IOC

### 9.1 文件哈希

| **类型** | **值**                                                           | **来源**                   |
|----------|------------------------------------------------------------------|----------------------------|
| MD5      | df8a2c219c032bef70fe59d8d2ef393a                                 | DeepExtract 输入           |
| SHA256   | 07b8f25e7b536f5b6f686c12d04edc37e11347c8acd5c53f98a174723078c365 | DeepExtract 输入           |
| SHA1     | 2440b3ec995ace60d31dcfbd9e4f011baffad4ff                         | MalwareBazaar 精确条目补充 |

### 9.2 高特异性主机 IOC

| **类型**     | **值**                               | **说明**                                 |
|--------------|--------------------------------------|------------------------------------------|
| Mutex        | \_x_X_UPDATE_X_x\_                   | 固定模式互斥体                           |
| Mutex        | \_x_X_PASSWORDLIST_X_x\_             | 凭据收集模式互斥体                       |
| Mutex        | \_x_X_BLOCKMOUSE_X_x\_               | 输入阻断模式互斥体                       |
| Mutex        | `<configured_name>_PERSIST`         | 配置派生；实际前缀未知                   |
| File         | %TEMP%\NOIP.abc                      | No-IP 输出                               |
| File         | %TEMP%\MSN.abc                       | Windows Live/MSN 输出                    |
| File         | %TEMP%\FIREFOX.abc                   | Firefox 输出                             |
| File         | %TEMP%\IELOGIN.abc                   | RAS 输出                                 |
| File         | %TEMP%\IEPASS.abc                    | IE Protected Storage 输出                |
| File         | %TEMP%\IEAUTO.abc                    | IE Storage2 输出                         |
| File         | %TEMP%\IEWEB.abc                     | Credential Manager 输出                  |
| File         | %TEMP%\xxxyyyzzz.dat                 | Windows Live/MSN 辅助输出                |
| File         | XX--XX--XX.txt                       | 配置序列化文件名；具体目录由调用路径决定 |
| Resource     | XX-XX-XX-XX                          | 配置资源名称                             |
| String       | ####@####                           | 配置分隔符                               |
| Entropy GUID | abe2869f-9b47-4cd9-a358-c22904dba7f7 | DPAPI 可选熵材料                         |

### 9.3 网络 IOC

**无已确认网络 IOC。** 外层已审代码未呈现 URL、域名、IP、端口或明确网络客户端调用。其他 CyberGate/Rebhip 样本使用的基础设施不得并入本样本 IOC。

## 10. MITRE ATT&CK 映射

| **ID**    | **技术**                                      | **本样本证据**                         | **置信度**     |
|-----------|-----------------------------------------------|----------------------------------------|----------------|
| T1140     | Decode Files or Information                   | XOR 0xBC 解码配置                      | 高             |
| T1547.001 | Registry Run Keys / Startup Folder            | HKLM/HKCU Run 与 Policies Run          | 高             |
| T1547.014 | Active Setup                                  | Installed Components 下写 StubPath     | 高             |
| T1112     | Modify Registry                               | 创建/修改持久化键值                    | 高             |
| T1055.002 | Portable Executable Injection                 | 手工 PE 映射、远程写入、远程线程       | 高             |
| T1555.003 | Credentials from Web Browsers                 | Firefox、IE Protected Storage/Storage2 | 高             |
| T1555.004 | Windows Credential Manager                    | CredEnumerateA 与 CredentialBlob/DPAPI | 高             |
| T1070.004 | File Deletion                                 | Shell 进程中循环删除原文件             | 高             |
| T1497.001 | Virtualization/Sandbox Evasion: System Checks | VM、沙箱、环境特征检查                 | 高（配置控制） |
| T1622     | Debugger Evasion                              | 调试器、PEB、内核调试设备检查          | 高（配置控制） |

## 11. 检测建议

### 11.1 静态与文件检测

- 优先封禁 SHA256，并对三组固定互斥体、资源名、分隔符、固定输出文件和 DPAPI GUID 组合匹配。

- 单个字符串可能被仿冒；检测规则应要求多组高特异性字符串共同出现。

- 搜索 %TEMP% 中固定 .abc/.dat 文件，同时关联创建进程、文件内容和后续删除行为。

### 11.2 行为检测

- 进程访问链：OpenProcess → VirtualAllocEx → WriteProcessMemory → CreateRemoteThread，并关注写入内容以 MZ 开头或包含 PE 结构。

- 异常宿主：explorer.exe 或非预期宿主出现私有可执行内存、重定位 PE、线程起始地址落在非映像内存。

- 注册表：监控 Run、Policies Run 和 Active Setup StubPath 指向用户可写目录、临时目录或无签名可执行文件。

- 凭据访问：非浏览器/非系统组件读取 Firefox 旧凭据文件、IE Storage2、Credential Manager，并调用 CryptUnprotectData。

- 输入阻断：未知进程同时安装 WH_KEYBOARD_LL 与 WH_MOUSE_LL 并持续消费消息循环。

## 12. 应急处置建议

1. 立即隔离命中哈希或高置信行为链的主机，避免先重启导致内存与注入证据丢失。

2. 采集内存、进程树、模块/线程、打开句柄、互斥体、注册表和 %TEMP% 文件。

3. 对 explorer.exe 与异常宿主执行内存映像提取，识别外层模块和内层 PE，分别计算哈希。

4. 将受影响主机上的 No-IP、Windows Live、Firefox、IE、RAS、Credential Manager 相关凭据视为可能泄露；在干净设备上轮换密码、Token 与远程访问凭据。

5. 取证完成后清理 Run、Policies Run、Active Setup、异常安装目录、固定输出文件和派生互斥体；重启后复核是否仍有注入。

6. 补充原始 PE/资源或独立静态资源导出，恢复 101 项配置和内层 PE；在隔离环境进一步确认网络行为。

## 13. 家族归因

### 13.1 归因结论

精确 SHA256 在 MalwareBazaar 中标记为 CyberGate，并给出与本地输入一致的 MD5、SHA256 以及补充 SHA1。Cisco Talos 将 CyberGate 描述为亦称 Rebhip，并在公开 IOC 中列出与本样本完全一致的三组固定互斥体、Run/Policies Run 以及 \<MUTEX\>\_PERSIST 结构。Elastic 的进程注入技术文章使用同一 SHA256 作为 PE Injection 示例。

因此，本报告将该样本高置信度归入 CyberGate / Rebhip 代码系。但归因只说明代码谱系和外部标签，不代表该家族公开描述的所有功能都已在本样本外层代码中确认。例如 C2、远程控制协议、截图、音视频和键盘记录均不能由当前静态输入直接确认。

### 13.2 外部参考

1. MalwareBazaar，精确 SHA256 样本条目：https://bazaar.abuse.ch/sample/07b8f25e7b536f5b6f686c12d04edc37e11347c8acd5c53f98a174723078c365/

2. Cisco Talos，Threat Round-up for Apr 14 - Apr 21：https://blog.talosintelligence.com/threat-roundup-0414-0421/

3. Cisco Talos，Threat Roundup for August 9 to August 16：https://blog.talosintelligence.com/threat-roundup-0809-0816/

4. Elastic，Ten process injection techniques：https://www.elastic.co/blog/ten-process-injection-techniques-technical-survey-common-and-trending-process

5. MITRE ATT&CK：https://attack.mitre.org/

## 14. 未决缺口与边界

| **Gap**               | **影响**                                                              | **阻塞报告** |
|-----------------------|-----------------------------------------------------------------------|--------------|
| 资源正文不可访问      | 不能恢复 101 项实际配置、安装路径/文件名、配置互斥体、开关、C2 与端口 | 否           |
| 内层 PE 字节不可访问  | 不能说明内层 PE 的导入、入口、TLS、网络和控制功能                     | 否           |
| IE 版本比较字符未呈现 | 不能给出精确支持的 IE 主版本                                          | 否           |
| 未执行动态分析        | 不能确认运行时权限、写入/解密/注入是否成功                            | 否           |

## 15. 关键函数证据索引

| **函数**        | **地址** | **作用**                           | **关键行**  |
|-----------------|----------|------------------------------------|-------------|
| F500 start      | 未提供   | 模式分派、凭据输出、安装和注入调度 | L0042-L0195 |
| F309 sub_404460 | 0x404460 | 资源读取                           | L0010-L0020 |
| F310 sub_4044D4 | 0x4044D4 | 配置分割                           | L0027-L0055 |
| F307 sub_4042DC | 0x4042DC | XOR 0xBC                           | L0017-L0033 |
| F325 sub_40491C | 0x40491C | 101 字段类型转换与装载             | L0008-L0071 |
| F394 sub_406B54 | 0x406B54 | 安装、复制和持久化                 | L0073-L0255 |
| F494 sub_40B118 | 0x40B118 | 反分析总控                         | 函数全文    |
| F495 sub_40B3C0 | 0x40B3C0 | 目标选择与注入调度                 | L0039-L0115 |
| F302 sub_403EC0 | 0x403EC0 | 构造 PE、重定位、导入和节权限      | L0032-L0098 |
| F304 sub_4040F4 | 0x4040F4 | 远程分配、写入和入口执行           | L0019-L0056 |
| F480 sub_409AD4 | 0x409AD4 | No-IP 凭据                         | L0019-L0032 |
| F482 sub_409D28 | 0x409D28 | Windows Live/MSN                   | L0039-L0093 |
| F483 sub_409EF8 | 0x409EF8 | Firefox NSS                        | L0150-L0355 |
| F440 sub_408238 | 0x408238 | RAS 凭据                           | L0059-L0165 |
| F464 sub_408E58 | 0x408E58 | IE Storage2/DPAPI                  | L0048-L0147 |
| F465 sub_40930C | 0x40930C | Credential Manager/DPAPI           | L0041-L0113 |
| F489 sub_40AFB0 | 0x40AFB0 | Shell 进程与删除调度               | L0022-L0034 |
| F486 sub_40AE94 | 0x40AE94 | 循环删除文件                       | L0005-L0012 |

## 附录 A：YARA 静态筛查规则

```yara
rule Win32_CyberGate_Rebhip_07b8_Static
{
    meta:
        description = "CyberGate/Rebhip-like sample with matching mutex, config and credential artifacts"
        author = "OpenAI"
        date = "2026-07-13"
        sha256 = "07b8f25e7b536f5b6f686c12d04edc37e11347c8acd5c53f98a174723078c365"

    strings:
        $m_update = "_x_X_UPDATE_X_x_" ascii
        $m_password = "_x_X_PASSWORDLIST_X_x_" ascii
        $m_block = "_x_X_BLOCKMOUSE_X_x_" ascii
        $cfg_resource = "XX-XX-XX-XX" ascii
        $cfg_delimiter = "####@####" ascii
        $cred_noip = "NOIP.abc" ascii
        $cred_msn = "MSN.abc" ascii
        $cred_firefox = "FIREFOX.abc" ascii
        $cred_iepass = "IEPASS.abc" ascii
        $cred_ieauto = "IEAUTO.abc" ascii
        $cred_ieweb = "IEWEB.abc" ascii
        $entropy_guid = "abe2869f-9b47-4cd9-a358-c22904dba7f7" ascii

    condition:
        uint16(0) == 0x5A4D and
        filesize < 5MB and
        2 of ($m_*) and
        all of ($cfg_*) and
        3 of ($cred_*) and
        $entropy_guid
}
```

该规则用于高特异性静态筛查，不应替代哈希、行为和内存检测。部署前应在组织样本集与良性软件集上测试误报率。

## 附录 B：最终判断

该样本的外层模块已经形成清晰、可达且证据闭环的恶意链：

**配置解码 → 模式分派 → 反分析 → 自复制与注册表持久化 → 多来源凭据窃取 → 固定临时文件落盘 → 手工 PE 注入 → 远程自删除**

当前最重要的处置重点是固定互斥体、固定输出文件、三类注册表持久化和 PE 注入行为。实际 C2 与内层功能必须通过资源/内层 PE 的独立静态导出或隔离动态分析补全，不能从家族通用信息推断。
