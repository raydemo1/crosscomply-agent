# 模型实验配置

普通应用、原模型评估和生产核验继续使用根目录 `.env` 与现有环境变量。实验模型使用根目录被 Git 忽略的 `.env.scu-ab`，只在指定的子进程内生效。

运行实验时，在已有 Python 评测命令前加 `scripts/with_experiment_model.py --`，例如：

```powershell
& 'D:\miniconda\miniconda\python.exe' scripts\with_experiment_model.py -- scripts\evaluate_qa_candidates.py --real --case eval_cross_border_008 --output data\review_runs\example-qa.json
```

该子进程将配置中的 Agent 和语义核验模型都切换到实验服务，并仅对实验服务域名绕过代理；其他地址继续使用原有代理设置。实验服务单次请求超时设为180秒、内部结构化流式请求总超时设为240秒；退出后不会改变当前终端或应用配置。评测脚本需要使用配置中的模型 ID，启动器不能覆盖脚本内部硬编码的其他模型名。要用原模型进行最终评估，直接运行原来的 Python 命令，不加实验启动器。新旧模型、提示词和所用证据都应在评测记录中列明；若检索过程、时间或证据不同，不能只将结果差异归因于提示词。

当前实验模型为 `DeepSeek-V4.1-Flash-1017`。2026-10-27 的小规模验证中，简单对话、流式工具调用及工具结果续接成功；与另一测试并行时，流式结构化 JSON 发生断流、普通流式文本返回 HTTP 504，真实 QA 的工具调用重试后返回 HTTP 502。随后单独运行同一 QA 案例完成，但答案仍有适用条件问题。单个成功样例不能证明稳定性，也无法确定并发上限；结构化 JSON 尚未在单独运行条件下复测。此前 `4q/DeepSeek-V4-Flash` 在同一服务上的结果只适用于该模型。

`.env.scu-ab` 含密钥，仅保留在本机；不要提交、分享或写入评测报告。实验记录保存模型请求与业务输入，运行前确认输入适合发送至实验服务。
