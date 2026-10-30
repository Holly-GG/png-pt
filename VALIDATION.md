# 验证记录

2026-10-30，在 Linux / Python 3.12 云环境执行：

- `pip install -r requirements.txt` 成功，`pip check` 无损坏依赖。
- `python -m unittest discover -s tests -v`：8 项通过，验证确认门槛、文件保护、重复运行、配置、日志及输出不变量；使用确定性测试蒙版，不冒充真实模型效果测试。
- IS-Net 权重通过 rembg 的下载校验，实际完成 CPU 推理及 closed-form alpha matting，生成 700×700 RGBA PNG，透明角落及不透明主体断言通过。
- `app` 模块与 Tkinter 导入通过；当前没有图形显示服务器，未实测桌面点击流程。
- 当前不是 Windows，尚未在 Windows 10/11 实机执行 `start.bat`。
- 没有用户实际产品照片，未完成真实照片效果验收，也未启动用户图片的批量处理。

可重跑 `python tests/smoke_model.py` 验证实际模型链路。首次使用需要联网下载模型，后续处理复用本地模型。
