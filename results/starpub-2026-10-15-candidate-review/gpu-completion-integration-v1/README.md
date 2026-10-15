# 首次集成验证的准备错误

完整 mapping 补丁序列已从固定上游源码重现实际测试内核的 33 个文件。后续 async-only 对照误用空目录，未先提取上游文件；应用器报 `refusing to overwrite local kernel edit`，该准备流程退出码 1。没有运行控制/分配接口回归。

原始错误、命令及空进程组审计保留在此；修正准备步骤后的独立 [v2 记录](../gpu-completion-integration-v2/README.md) 包含完整验证。
