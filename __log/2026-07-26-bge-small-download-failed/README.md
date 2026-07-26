# 事件：bge-small-zh-v1.5 首次下载失败

**日期**：2026-07-26
**阶段**：Phase 1 · T1（环境准备）
**影响**：Phase 1 不受影响；Phase 2 embedding 层需要该模型，须在 Phase 2 开工前解决

## 问题场景

Phase 1 T1 步骤 5/5：`SentenceTransformer('BAAI/bge-small-zh-v1.5')` 首次下载，用于路由 embedding 层。

## 问题本身

- `curl -I https://hf-mirror.com` 返回 HTTP 200（TCP + HTTP + TLS 层都通）
- `curl -I https://huggingface.co` 返回 HTTP 200（直连也通）
- Python `requests / huggingface_hub` 通过 `SentenceTransformer` 内部下载时抛：
  ```
  OSError: We couldn't connect to 'https://hf-mirror.com' to load the files,
  and couldn't find them in the cached files.
  ```
- 环境变量 `HF_ENDPOINT=https://hf-mirror.com` 已设

**推测原因**（未证实）：
1. hf-mirror.com 的模型仓库路径 `/BAAI/bge-small-zh-v1.5/resolve/main/...` 该时段瞬时抖动
2. huggingface_hub 内部走 HTTP/2 或特定 UA，与 curl 表现不同
3. HF_ENDPOINT 只影响 API host，模型文件下载走的是 CDN，可能没走镜像

## 已确认的事实

- pip 依赖 **全部装成功**（hello-agents / pydantic 2.13.4 / sentence-transformers 5.6.1 / torch 2.13.0 / PyYAML 6.0.3 / pytest 9.1.1 / python-dotenv / requests / numpy 2.5.1）
- Phase 1（T3-T8）**完全不使用** embedding 层，可以先跳过
- Phase 2 起才用到 `EmbedLayer`（`src/skillforge/router/embed.py`）

## 备选方案（Phase 2 开工前选一）

1. **重试 hf-mirror**（成本 0）：抖动过去后大概率能过
2. **手动下载模型 tarball + 本地路径加载**：
   - 从 https://hf-mirror.com/BAAI/bge-small-zh-v1.5 手动 wget 到 `~/.cache/huggingface/hub/`
   - `SentenceTransformer('/local/path')` 指定本地
3. **切换 modelscope（阿里模型库）**：
   - `pip install modelscope`
   - 模型 ID：`AI-ModelScope/bge-small-zh-v1.5` 或 `Xorbits/bge-small-zh-v1.5`
   - 修改 `router/embed.py` 走 modelscope
4. **换更小模型**：如 `text2vec-base-chinese`（体积更小，速度稍差但方案书未硬绑定 bge-small）——**方案书 v3 §4.3 允许换检索卡片编码模型**，若换需更新方案书。
5. **走代理**：如果用户已有可用代理（clash/v2ray），临时 `HTTPS_PROXY` 环境变量指过去下载一次。

## 推荐执行顺序

- Phase 2 前先试方案 1（重试）
- 3 次失败后走方案 2（手动 wget）
- 仍不行走方案 3（modelscope）

## 当前状态

- **Phase 1 继续推进**，T1 视为"依赖装完，模型待补"
- Phase 2 T2.1（embedding 层实现）前必须解决
