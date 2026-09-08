# 本地 OCR 遥测边界与运行验证

## 结论与范围

已确认并修正：本地 OCR 初始化未显式禁用 ONNX Runtime 遥测事件。相同回归测试修复前 3 项失败、修复后 3 项通过；完整测试及真实 OCR 窗口退出检查均正常结束。

无法确认：历史偶发原生退出崩溃是否彻底消除。本轮修复前完整测试也曾正常退出，不能把这项间歇故障写成确定性复现或已根除。没有更新已安装应用，也未验证 Windows 或重新打包后的应用。

## 原始证据与根因

- 本机两份 Python 3.12 崩溃记录（2026-09-08 07:02:28、07:05:19）包含 `onnxruntime::PosixTelemetry::Shutdown`、`LogManagerImpl::FlushAndTeardown`、`HttpClientManager::cancelAllRequests`；故障线程包含 `DebugEventSource::DispatchEvent`、`HttpResponseDecoder` 和递归互斥锁调用。记录指向 ONNX Runtime 遥测子系统的进程退出清理路径，不支持归因于 Qt 平台插件，也不表示禁用接口自身发生故障。
- 本机 ONNX Runtime 为 1.29.0；安装包提供 `disable_telemetry_events()`。共享 `RapidOcrProvider` 构造函数原先直接加载 OCR 包，没有先调用该接口；图形界面和验收执行器均经过该适配器。
- 崩溃栈不证明业务文件内容曾被上传。本次未进行网络抓包，不能据此认证整个进程完全无网络活动。

## 最小修改与红绿验证

生产代码仅在共享构造函数内导入 ONNX Runtime，并在加载 `rapidocr_onnxruntime` 前调用 `disable_telemetry_events()`；不改识别算法、模型、金额计算、数据库或业务验收门控。

新增 `tests/unit/test_platform_rapidocr.py` 后，先在原生产代码上实际执行：

```sh
uv run pytest -o addopts='' -q tests/unit/test_platform_rapidocr.py
```

修复前 3 项失败：直接构造和默认工厂均只记录到 `load_ocr`，未出现要求的 `disabled`；模拟禁用失败时，默认工厂仍返回提供器。失败对应目标行为，不是依赖缺失或测试环境错误。

修复后原测试文件未修改，执行相同命令得到 3 passed。默认工厂在禁用接口抛出 `RuntimeError` 时沿用既有不可用处理，不继续提供 OCR。

## 相关运行结果

| 检查 | 结果 |
| --- | --- |
| 修复前完整现有测试 | 1001 passed、3 skipped，退出码 0，90.70 秒 |
| 修复后完整现有测试，排除新增遥测测试 | 1001 passed、3 skipped，退出码 0，89.83 秒 |
| 修复后真实 OCR、PDF 页面与导入相关测试 | 66 passed，退出码 0，2.83 秒 |
| 修复后全部测试，启用真实 OCR | 1005 passed、2 skipped，退出码 0，90.65 秒 |
| 三个独立进程：真实 OCR、应用事件循环、关闭窗口 | 全部退出码 0，均识别 `Contract 120000` |
| Ruff 与差异空白检查 | 通过 |

完整测试命令：

```sh
JIADUN_TEST_REAL_OCR=1 QT_QPA_PLATFORM=offscreen uv run pytest -o addopts='' -q
uv run ruff check src scripts tests
git diff --check
```

完整测试的 1 条警告来自故意构造重复 ZIP 条目的恢复测试。窗口探针使用隔离临时项目和合成图片；无屏幕模式的平台提示及探针预建应用对象触发的 DPI 提示不计为生产缺陷。该探针不替代打包应用的人机验收。

## 资源与剩余验证

测试与三个探针进程均已结束；隔离项目使用临时目录自动清理，未删除用户资料或系统级缓存。收尾进程检查未发现本次 pytest 或窗口探针残留。私有探针脚本保留在被 Git 忽略的本地证据区。

后续仍需验证打包应用、Windows 环境及更长时间的重复退出；任何再次出现的原生崩溃应保存新崩溃记录后重新定位。当前结论仅覆盖已执行的测试，不构成生产发布或正式业务验收。

## 独立复核及差异处理

- 角色：`independent_reviewer`；模型：Luna；推理档位：max；任务标识：`review_ocr_telemetry`。只读复核结论为有条件通过，无阻断本次最小修复的严重问题。
- 独立复核重新读取两份原始崩溃记录、共享入口及所有调用路径，并执行定向测试（3 passed）、全量 Ruff、差异检查和三个真实 OCR 独立进程退出检查（全部退出码 0）。完整套件和 Qt 探针结果来自主任务实际运行，未被重复算作独立执行。
- 接受措辞修正：明确为遥测子系统的进程退出清理路径；不将相关性写成唯一根因完全证明。
- 接受证据限制：新增单元测试证明 Python 层调用顺序和默认工厂门控，不证明原生线程行为。真实识别与退出证据来自独立进程探针，也不等于长期故障根除。
- 暂缓异常类型统一：禁用接口抛出 `RuntimeError` 时，默认工厂返回 `None`，直接构造则原样抛出；未来运行时缺少该 API 时可能抛出 `AttributeError`。当前依赖确实提供 API，GUI 和默认生产路径已有不可用处理；本次不增加兼容层。若扩大支持范围，应先补复现和验收条件再修改。
