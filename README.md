# Screen Watch OCR

电脑屏幕指定区域实时监控工具。它先用 `mss` 截取多屏区域，再按目标类型做匹配：

- `template`: OpenCV 模板匹配，适合头像、Logo、固定图标、建筑截图等视觉特征。
- `pixel`: 指定坐标颜色匹配，适合红点、状态灯、固定 UI 标记。
- `ocr_text`: RapidOCR 3.9.0 + ONNXRuntime + PP-OCRv6 small OCR，适合中文、英文、数字、按钮文案。

## 结论

当前最稳的落地路线不是把每一帧丢给小型 VLM，而是“截图 + 规则视觉检测 + 轻量 OCR”：

1. 实时报警主链路：`mss` 多屏截图 + OpenCV 模板/像素检测 + RapidOCR 3.9.0。
2. 新一代小模型增强链路：PP-OCRv5 / PaddleOCR-VL / Qwen2.5-VL / GOT-OCR2 适合低频复核、复杂画面解释、离线校验，不适合默认逐帧实时监控。
3. 真要识别任意头像/建筑/开放词表目标，再加一个本地 VLM 服务做低频二次确认；报警前先由模板或像素粗筛，避免 GPU/CPU 被连续帧拖死。

原因很简单：监控要“快、可解释、低误报”。固定目标用模板/像素比 VLM 更快更稳定；文字用 PP-OCR 系列小模型比通用 VLM 更少幻觉；VLM 留给复杂图像语义。

## 调研来源与取舍

| 方案 | 证据 | 优点 | 风险 | 本项目选择 |
| --- | --- | --- | --- | --- |
| PP-OCRv5 / PaddleOCR | GitHub 显示 PaddleOCR v3.7.0 于 2026-06-11 发布，支持 100+ 语言；PP-OCRv5 论文称 5M 参数可接近十亿参数 VLM 的 OCR 表现 | 精度高，模型小，定位准 | Paddle/Python 环境较重；实时屏幕监控安装成本高 | 作为正式升级路线，当前先用 ONNX 版本 |
| RapidOCR 3.9.0 + ONNXRuntime | 官方 GitHub 说明它把 PaddleOCR 模型转 ONNX，便于跨平台离线部署；本机实测加载 PP-OCRv6 small det/rec 模型 | 安装比 Paddle 轻，CPU 可跑，适合本地工具 | 第一次初始化会有模型加载开销 | 默认 OCR 后端 |
| PaddleOCR-VL-1.6 | 2026-06-02 论文，0.9B 基线升级，文档解析分数高 | 复杂文档、表格、版面好 | 0.9B 仍不适合高频逐帧 | 低频复核候选 |
| Qwen2.5-VL 3B/7B | 技术报告说明支持识别、定位、文档解析、视觉代理 | 能识别建筑、物体、复杂语义 | 逐帧成本高；需要 GPU/推理服务 | 只建议作二次确认 |
| GOT-OCR2.0 | 580M OCR-2.0 模型，支持文本、公式、图表等 | 泛 OCR 能力强 | 官方环境偏 CUDA，CPU 部署不如 ONNX OCR 简单 | 备选复核模型 |
| SikuliX/PyAutoGUI/OpenCV 成熟案例 | SikuliX 使用截图 + OpenCV 找图；PyAutoGUI `confidence` 也依赖 OpenCV | 固定 UI 自动化成熟、可解释 | 大屏全图扫描慢，需限制区域 | 采用其核心思路，但用 `mss` 解决多屏和速度 |
| mss | 官方说明是快速、跨平台、多屏截图库，能和 NumPy/OpenCV 集成 | 无窗口绑定，支持多屏区域 | 高 DPI 要先导入 mss，显示缩放需实测 | 默认截图层 |

主要来源：

- PaddleOCR GitHub: https://github.com/PaddlePaddle/PaddleOCR
- PP-OCRv5 论文: https://arxiv.org/abs/2603.24373
- PaddleOCR-VL-1.6 论文: https://arxiv.org/abs/2606.03264
- RapidOCR GitHub: https://github.com/RapidAI/RapidOCR
- RapidOCR PyPI: https://pypi.org/project/rapidocr/
- mss PyPI: https://pypi.org/project/mss/
- PyAutoGUI screenshot docs: https://pyautogui.readthedocs.io/en/latest/screenshot.html
- GOT-OCR2.0 GitHub: https://github.com/Ucas-HaoranWei/GOT-OCR2.0
- Qwen2.5-VL 技术报告: https://arxiv.org/abs/2502.13923
- Sikuli 论文: https://codeblab.com/wp-content/uploads/2010/01/sikuli.pdf

## 使用

```powershell
cd E:\Project\Common\screen-watch-ocr
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m screen_watch app
.\.venv\Scripts\python.exe -m screen_watch list-monitors
```

## 图形应用

发布时可用 `ScreenWatchOCR.spec` 打包成独立应用。运行后的配置会自动放到当前 Windows 用户的应用数据目录，不依赖项目目录。

最常用流程：

1. 点 `上传图片` 添加一个或多个模板图。
2. 或用系统截图工具截小图，回到应用按 `Ctrl+V`，可连续粘贴多张。
3. 上方区域会以固定缩略图网格展示模板，默认约两排、每排五张；点缩略图后可删除。
4. 选择 `配置位` 1-5；关闭时会自动保存当前配置位的图片、屏幕、区域和匹配参数，下次自动恢复上次配置位。
5. 选择要监控的屏幕；区域留空宽高就是整屏，填宽高就是只扫指定区域。
6. 在 `蜂鸣秒` 填 3、5 等持续时间；蜂鸣没结束时再次命中不会重新计时。
7. 在 `截图最多张` 填 10、20、50、100 等上限；产生新截图时会自动只保留最新的 N 张。
8. 点 `开始监控`，命中后蜂鸣、写日志、保存带红框截图。

点击窗口右上角关闭按钮会缩小到系统托盘；托盘右键菜单里有 `退出`，只有点它才会真正结束程序。

应用数据目录：`C:\Users\Wes\AppData\Local\ScreenWatchOCR`

截图目录：`C:\Users\Wes\AppData\Local\ScreenWatchOCR\screenshots`

右侧参数：

- `阈值`: 模板匹配分数，0-1，越高越严格，误报少但可能漏掉缩放/压缩后的图。
- `缩放`: 模板按哪些倍率去找，例如 `1.0` 或 `0.9,1.0,1.1`。
- `间隔ms`: 每轮截屏扫描之间的间隔，越小越实时。
- `同图冷却秒`: 同一屏幕、同一张模板命中后，多少秒内不重复报警和存图。
- `蜂鸣秒`: 一次有效报警后蜂鸣持续多久；蜂鸣未结束时再次命中不会重置时长。
- `截图最多张`: 截图目录最多保留多少张；下次保存截图时自动删旧留新。

打包独立应用：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm ScreenWatchOCR.spec
```

生成演示配置和素材：

```powershell
.\.venv\Scripts\python.exe -m screen_watch make-demo --out demo
.\.venv\Scripts\python.exe -m screen_watch self-test --out evidence\selftest
```

截取一个真实屏幕区域：

```powershell
.\.venv\Scripts\python.exe -m screen_watch screenshot --monitor 1 --left 0 --top 0 --width 640 --height 360 --out evidence\real_screen.png
```

监控一次或持续监控：

```powershell
.\.venv\Scripts\python.exe -m screen_watch once --config config.json
.\.venv\Scripts\python.exe -m screen_watch watch --config config.json
```

## 配置样例

```json
{
  "poll_interval_seconds": 0.25,
  "cooldown_seconds": 2,
  "regions": [
    { "name": "left-top", "monitor": 1, "left": 0, "top": 0, "width": 640, "height": 360 },
    { "name": "second-screen", "monitor": 2, "left": 100, "top": 100, "width": 800, "height": 500 }
  ],
  "targets": [
    { "name": "boss-avatar", "kind": "template", "path": "templates/boss.png", "threshold": 0.91, "scales": [0.9, 1.0, 1.1] },
    { "name": "red-dot", "kind": "pixel", "x": 50, "y": 50, "rgb": [255, 0, 0], "tolerance": 12 },
    { "name": "warning-text", "kind": "ocr_text", "text": "WARNING", "min_score": 0.4 }
  ],
  "alarm": {
    "beep": true,
    "save_dir": "evidence/alerts",
    "jsonl": "evidence/alerts.jsonl"
  }
}
```

`regions[].monitor` 使用 `list-monitors` 输出的编号。`mss` 的 `0` 是所有显示器合并后的虚拟画布，本工具故意不用它，避免多屏坐标误会。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe -m screen_watch app --smoke-test
```

## 后续升级

- 如果 OCR 区域很多，先裁小区域、降低轮询频率；不要让 OCR 跑全屏。
- 如果目标不是固定图标，而是“任意同类物体”，接入 Qwen2.5-VL/Florence/SAM 类服务做低频复核。
- 如果要 30 FPS 级监控，改成 DXGI/Desktop Duplication 或 Windows Graphics Capture；`mss` 足够做 2-3 屏低频监控。
