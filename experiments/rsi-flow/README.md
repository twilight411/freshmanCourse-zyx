# rsi-flow：私服依赖修复与结构验证

基于 [ymxiong/rsi-flow](https://github.com/ymxiong/rsi-flow) 的 `dev_rsi` 固定提交 `93cc223de5bc8f0510801796a46a8ae391725be9`。本目录提供可审阅补丁、真实引擎执行证据及 Python 复现入口。

## 一条命令复现

需要 Git、JDK 和 Maven 已加入 PATH。本次验证环境为 JDK 17、Maven 3.8.9、Windows；其他操作系统尚未实测。Python 3.10+。

```bash
python scripts/run_rsi_flow_validation.py --settings "本机课程配置文件/settings.xml"
```

课程提供的 `settings.xml` 已验证可访问私服。脚本将固定版本克隆到 `.work/rsi-flow-authenticated`，应用 [authenticated-validation.patch](authenticated-validation.patch)，使用独立 Maven 缓存和你指定的外部配置。第一次运行需要联网下载依赖，不调用模型。日志和统计保存在 `experiments/rsi-flow/runs/北京时间时间戳/`。

也可通过本机环境变量 `RSI_MAVEN_SETTINGS` 指定配置路径。配置文件留在仓库外；脚本不会复制或上传它，并会过滤日志中的 server 账号、密码和 Basic 凭证。仓库仅提供 [无凭证模板](maven-settings.example.xml)，需自行获得授权账号。

脚本不修改原工作区的 rsi-flow，也不覆盖已有不同版本的验证副本。补丁更新后，如提示版本不同，请先把相应 `.work/` 副本移至备份位置再运行。可用 `--cache 路径` 指定其他依赖缓存；Windows 上建议放在同一磁盘。

## 修复了什么

| 问题 | 处理 |
|---|---|
| 原版三个依赖需要私服授权 | 使用课程配置中与仓库 ID `releases` 匹配的 server 凭证；三个原版 POM 请求均返回 200 |
| 担心依赖替代掩盖原版问题 | 正式方案保留 `auto-config:1.0.6`、`easyfile:0.0.3`、`scheduler-core:0.1.2` 及原版定时 Holder，未替换为占位依赖 |
| Java 17 与旧 JaCoCo/CGLIB 不兼容 | 更新 JaCoCo 至 0.8.12、Surefire 至 3.2.5；JDK 9+ 测试开放 `java.lang` 访问 |
| Windows 中文路径下覆盖率 agent 启动失败 | 默认仅运行测试，覆盖率统计通过 `-Pcoverage` 单独开启；本轮不报告代码覆盖率 |
| Spring Boot 接入模块缺少编译依赖 | 显式增加 `spring-boot` 依赖，并对根项目执行八模块打包检查 |
| 关键词跨 4096 字符块边界漏检 | 分块扫描增加关键词重叠，按原文行提取证据 |
| 证据长度漏计换行符 | 将行间换行纳入 4096 字符预算，超预算整行不传递 |

401 的原因是原先没有使用匹配的私服认证配置。当前已验证原版依赖下载、编译和普通流程执行。定时类可加载，但定时/分布式调度服务没有获得实际运行证明。普通结构测试排除自动启动的定时样例资源，以免加载配置时产生后台任务；这不删除定时实现。

## 结构与验证范围

```text
start → AGENT_ROUTE ─短输入→ AGENT_SHORT ──────────┐
                    └长输入→ FLOW_AGENT_RETRIEVE ──┤→ AGENT_VERIFY → end
                               └ AGENT_CHUNK → AGENT_DRAFT
```

- **11 项长输入与结构测试**：短输入顺序；长输入嵌套；4096/4097 字符分支；长短输入无证据；跨块关键词；证据预算；超预算单行；子流程独立入口；连续调用释放上下文。测试断言真实 Java 引擎的节点调用顺序和输出，不是另写一个模拟引擎。
- **3 项原版依赖检查**：外部模型配置类型实例化；原版调度类和两个 Holder 可加载；原版文件工具可读 UTF-8 内容。类加载检查不等于调度服务执行测试。
- **6 项上游回归测试**：标准流程、嵌套流程、条件的两个分支、多节点、单节点。
- **8 项离线图检查测试**：正确图、悬空边、环、不可达节点、无出口节点、缺失事件实现、子流程参数契约不一致和递归子流程。另对两个 JSON 流程执行图检查。

长路径实测输入超过 130,000 字符。这里按 Java 字符长度和确定性字符串匹配检索；不是模型 token 预算、语义检索或 Spider/BIRD 成绩。LLM 节点和 Text-to-SQL 数据集仍需单独接入。

复现脚本检查 20 个 Java 测试全部通过、0 跳过，再执行根项目打包（打包阶段不重复测试），保存测试方法名、执行路径、补丁与日志哈希。完整结果见 [结构验证报告](../../reports/rsi_flow_structure_validation.md)。最新有效记录见 [原版依赖验证索引](latest-authenticated.json)。

## 不访问私服的备选方案

```bash
python scripts/run_rsi_flow_validation.py --portable
```

该独立方案应用 [portable-validation.patch](portable-validation.patch)，仅使用公开 Central：移除 `auto-config`、改用项目内模型配置，文件工具降为公开的 0.0.2，定时功能移至需要私服的可选配置。它已通过独立缓存下的 20 项 Java 测试和 8 项图检查测试，证据见 `runs/20261117_221118/`。其 3 项构建边界测试与正式方案的 3 项依赖检查不同；两种方案不可混写为同一次测试。正常有课程配置时，优先使用上面的原版依赖方案。
