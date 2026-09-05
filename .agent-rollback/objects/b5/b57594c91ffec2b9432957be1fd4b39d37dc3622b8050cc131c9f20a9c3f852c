# 异常处理记录: PyTorch/CUDA 快速复跑显存回收竞态 (SIGSEGV 139)

- 日期: 2026-09-05
- 事件: PyTorch/CUDA 模块在连续高频执行 pytest 时偶尔发生 SIGSEGV (code 139)
- 问题场景: 在运行 `test_evolver_patch_validation_real_p0_gate_budget_hard_stop` 时加载 `sentence_transformers`，底层 PyTorch C-extension 初始化与上一进程的 CUDA context 回收发生毫秒级竞态。
- 问题本身: 进程直接崩溃退出，错误码 139 (SIGSEGV)。
- 备选方案:
  1. 在运行包含 embedding/torch 的全量测试时，指定 `CUDA_VISIBLE_DEVICES=""` 强制使用 CPU 避免 GPU 驱动上下文初始化抖动；
  2. 在连续测试间加入微小冷却等待；
  3. 实测 CPU 模式 `CUDA_VISIBLE_DEVICES="" uv run --no-sync pytest tests -q` 稳定 200 passed (7.17s)，默认模式 `uv run --no-sync pytest tests -q` 亦稳定 200 passed (5.74s)。
