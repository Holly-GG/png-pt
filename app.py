"""Windows desktop UI. Worker threads never call Tk directly."""
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk
from engine import Batch, atomic_json, load_config

ROOT = Path(__file__).resolve().parent


def checker_preview(path, size=(330, 300)):
    bg = Image.new('RGBA', size, 'white')
    draw = ImageDraw.Draw(bg)
    for y in range(0, size[1], 16):
        for x in range(0, size[0], 16):
            if (x // 16 + y // 16) % 2:
                draw.rectangle((x, y, x + 15, y + 15), fill='#dddddd')
    with Image.open(path) as im:
        im = im.convert('RGBa')
        im.thumbnail(size, Image.Resampling.LANCZOS)
        im = im.convert('RGBA')
        bg.alpha_composite(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
    return bg


class App:
    def __init__(self, window):
        self.window = window
        window.title('产品图片工作台 · 本地批量透明 PNG')
        window.geometry('1100x780')
        window.minsize(850, 650)
        self.events = queue.Queue()
        self.busy = False
        self.batch = None
        self.photos = []
        self.config_path = ROOT / 'config.json'
        self.input = tk.StringVar()
        self.output = tk.StringVar()
        self.status = tk.StringVar(value='选择目录，然后生成 3 张随机预览。确认效果后再批量处理。')
        self.stats = tk.StringVar(value='总数 0 | 已处理 0 | 成功 0 | 失败 0 | 跳过 0')
        body = ttk.Frame(window, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='产品图片工作台', font=('Microsoft YaHei UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(body, text='本地 IS-Net 抠图 · 保守精修 · 轮廓接触阴影 · 透明 PNG').pack(anchor='w', pady=(0, 15))
        paths = ttk.Frame(body)
        paths.pack(fill='x')
        paths.columnconfigure(1, weight=1)
        self.path_controls = []
        for row, (title, variable) in enumerate([('输入文件夹', self.input), ('输出文件夹', self.output)]):
            ttk.Label(paths, text=title).grid(row=row, column=0, padx=(0, 12), pady=5)
            entry = ttk.Entry(paths, textvariable=variable)
            entry.grid(row=row, column=1, sticky='ew', pady=5)
            button = ttk.Button(paths, text='选择…', command=lambda v=variable: self.browse(v))
            button.grid(row=row, column=2, padx=8)
            self.path_controls.extend([entry, button])
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=12)
        self.preview_button = ttk.Button(actions, text='① 生成随机预览（最多 3 张）', command=self.preview)
        self.preview_button.pack(side='left')
        self.confirm = ttk.Button(actions, text='② 确认效果，开始批处理', command=self.run, state='disabled')
        self.confirm.pack(side='left', padx=10)
        self.cancel_button = ttk.Button(actions, text='停止', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left')
        ttk.Button(actions, text='打开输出目录', command=self.open_output).pack(side='right')
        ttk.Label(body, text='参数在 config.json 中修改；下次生成预览时重新载入。棋盘格仅用于显示透明度。').pack(anchor='w')
        self.gallery = ttk.Frame(body)
        self.gallery.pack(fill='both', expand=True, pady=12)
        ttk.Label(body, textvariable=self.stats).pack(anchor='w')
        self.bar = ttk.Progressbar(body, mode='determinate')
        self.bar.pack(fill='x', pady=8)
        ttk.Label(body, textvariable=self.status, wraplength=1000).pack(anchor='w')
        ttk.Label(body, text='请逐张检查白色、反光、透明产品和细小文字；不满意时调整参数并重新预览。').pack(anchor='w', pady=8)
        for var in (self.input, self.output):
            var.trace_add('write', lambda *_: self.confirm.configure(state='disabled'))
        try:
            _, source, target = load_config(self.config_path)
            self.input.set(str(source))
            self.output.set(str(target))
        except Exception as exc:
            messagebox.showerror('配置错误', str(exc))
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(100, self.poll)

    def browse(self, variable):
        value = filedialog.askdirectory()
        if value:
            variable.set(value)

    def set_busy(self, busy):
        self.busy = busy
        self.preview_button.configure(state='disabled' if busy else 'normal')
        self.cancel_button.configure(state='normal' if busy else 'disabled')
        self.confirm.configure(state='disabled')
        for control in self.path_controls:
            control.configure(state='disabled' if busy else 'normal')

    def worker(self, action, kind):
        def target():
            try:
                value = action()
                self.events.put((kind, value))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=target, daemon=True).start()

    def preview(self):
        try:
            config, _, _ = load_config(self.config_path)
            if not self.input.get().strip() or not self.output.get().strip():
                raise ValueError('输入、输出路径不能为空')
            config['input_folder'] = str((ROOT / self.input.get()).resolve())
            config['output_folder'] = str((ROOT / self.output.get()).resolve())
            # Validate before persisting a selected path.
            from tempfile import TemporaryDirectory
            # Paths are absolute here, so config location does not affect validation.
            with TemporaryDirectory() as temp:
                candidate = Path(temp) / 'config.json'
                atomic_json(candidate, config)
                config, source, target = load_config(candidate)
            self.batch = Batch(config, source, target)
            atomic_json(self.config_path, config)
        except Exception as exc:
            messagebox.showerror('无法开始', str(exc))
            return
        for child in self.gallery.winfo_children():
            child.destroy()
        self.photos.clear()
        self.stats.set(f'总数 {len(self.batch.files)} | 已处理 0 | 成功 0 | 失败 0 | 跳过 0')
        self.bar['value'] = 0
        self.set_busy(True)
        self.status.set('准备本地模型，首次运行可能需要几分钟下载；图片不会上传。')
        self.worker(lambda: self.batch.preview(lambda text: self.events.put(('status', text))), 'preview')

    def run(self):
        if not self.batch or self.busy:
            return
        if not messagebox.askyesno('确认预览效果', '已检查预览中的边缘、颜色和接触阴影？\n确认后将处理整个目录，不会修改原图。'):
            return
        self.set_busy(True)
        self.worker(lambda: self.batch.run(True, lambda *args: self.events.put(('progress', args))), 'done')

    def cancel(self):
        if self.batch:
            self.batch.cancel.set()
            self.status.set('正在停止；当前图片结束后停止，请稍候。')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                elif kind == 'preview':
                    self.set_busy(False)
                    for source, output in value:
                        frame = ttk.Frame(self.gallery)
                        frame.pack(side='left', fill='both', expand=True, padx=4)
                        photo = ImageTk.PhotoImage(checker_preview(output))
                        self.photos.append(photo)
                        ttk.Label(frame, image=photo).pack()
                        ttk.Label(frame, text=source.name, wraplength=300).pack()
                        ttk.Button(frame, text='查看原尺寸预览', command=lambda p=output: os.startfile(p)).pack(pady=5)
                    if value and not self.batch.cancel.is_set():
                        self.confirm.configure(state='normal')
                        self.status.set('请检查预览；满意后点击“确认效果，开始批处理”。不满意可编辑配置并重新生成。')
                    elif not value:
                        total = len(self.batch.files)
                        self.stats.set(f'总数 {total} | 已处理 {total} | 成功 0 | 失败 0 | 跳过 {total}')
                        self.bar['maximum'] = total
                        self.bar['value'] = total
                        self.status.set('全部图片已通过原图、配置和输出校验，无需重复处理。')
                    else:
                        self.status.set('已停止，请重新生成预览。')
                elif kind == 'progress':
                    index, total, ok, fail, skip, name = value
                    self.bar['maximum'] = total
                    self.bar['value'] = index
                    self.stats.set(f'总数 {total} | 已处理 {index} | 成功 {ok} | 失败 {fail} | 跳过 {skip}')
                    self.status.set(name)
                elif kind == 'done':
                    self.set_busy(False)
                    ok, fail, skip = value
                    self.status.set(f'{"已停止" if self.batch.cancel.is_set() else "处理完成"}：成功 {ok}，失败 {fail}，跳过 {skip}。失败详情见输出目录 error_log.txt。')
                elif kind == 'error':
                    self.set_busy(False)
                    self.status.set('未完成：' + value)
                    messagebox.showerror('处理未完成', value + '\n若为图片处理错误，请查看输出目录 error_log.txt。')
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def open_output(self):
        target = Path(self.output.get())
        if target.is_dir():
            os.startfile(target)
        else:
            messagebox.showinfo('提示', '输出目录尚未创建。')

    def close(self):
        if self.busy:
            self.cancel()
            messagebox.showinfo('正在处理', '已请求停止，请等待当前任务结束后关闭窗口。')
        else:
            self.window.destroy()


if __name__ == '__main__':
    window = tk.Tk()
    App(window)
    window.mainloop()
