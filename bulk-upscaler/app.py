"""Bulk Upscaler: desktop app that AI-upscales up to 5 images at a time.

Run:  python app.py
"""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image

from upscaler import core

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # drag and drop is optional
    TkinterDnD = None

APP_DIR = Path(__file__).resolve().parent


class App:
    def __init__(self, root):
        self.root = root
        self.files = []
        self.events = queue.Queue()
        self.busy = False

        root.title("Bulk Upscaler")
        root.minsize(640, 520)
        pad = {"padx": 12, "pady": 6}

        # Image list
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text=f"Images (up to {core.MAX_BATCH})", font=("", 12, "bold")).pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear).pack(side="right")
        ttk.Button(top, text="Remove selected", command=self.remove_selected).pack(side="right", padx=6)
        ttk.Button(top, text="Add images...", command=self.pick_files).pack(side="right")

        cols = ("size", "result", "status")
        self.tree = ttk.Treeview(root, columns=cols, height=core.MAX_BATCH, selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.heading("size", text="Original")
        self.tree.heading("result", text="Upscaled to")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=240)
        for c, w in (("size", 100), ("result", 110), ("status", 140)):
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="x", **pad)

        hint = "Drag images here or click Add images." if TkinterDnD else "Click Add images to choose files."
        ttk.Label(root, text=hint, foreground="gray").pack(anchor="w", padx=12)
        if TkinterDnD:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", lambda e: self.add_files(root.tk.splitlist(e.data)))

        # Options
        opts = ttk.LabelFrame(root, text="Settings")
        opts.pack(fill="x", **pad)

        self.scale = tk.IntVar(value=4)
        ttk.Label(opts, text="Scale:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        for i, s in enumerate((2, 3, 4)):
            ttk.Radiobutton(opts, text=f"{s}x", variable=self.scale, value=s,
                            command=self.refresh_sizes).grid(row=0, column=1 + i, sticky="w")

        self.model_keys = {m.label: k for k, m in core.MODELS.items()}
        self.model = tk.StringVar(value=core.MODELS["photo"].label)
        ttk.Label(opts, text="Image type:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(opts, state="readonly", width=32, textvariable=self.model,
                     values=list(self.model_keys)).grid(row=1, column=1, columnspan=4, sticky="w")

        self.fmt = tk.StringVar(value="png")
        ttk.Label(opts, text="Save as:").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Radiobutton(opts, text="PNG (keeps transparency)", variable=self.fmt, value="png").grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(opts, text="JPG (smaller file)", variable=self.fmt, value="jpg").grid(row=2, column=3, columnspan=2, sticky="w")

        self.dpi = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Stamp 300 DPI (for print / Printify)", variable=self.dpi).grid(
            row=3, column=0, columnspan=4, sticky="w", padx=8, pady=4)

        # Output folder
        out = ttk.Frame(root)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="Save to:").pack(side="left")
        self.out_dir = tk.StringVar(value=str(Path.home() / "Pictures" / "Upscaled"))
        ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Browse...", command=self.pick_out_dir).pack(side="left")

        # Engine status
        eng = ttk.Frame(root)
        eng.pack(fill="x", **pad)
        self.engine_label = ttk.Label(eng)
        self.engine_label.pack(side="left")
        self.install_btn = ttk.Button(eng, text="Install AI engine", command=self.install_engine)
        self.refresh_engine()

        # Run
        run = ttk.Frame(root)
        run.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(run, maximum=core.MAX_BATCH)
        self.progress.pack(side="left", fill="x", expand=True)
        self.open_btn = ttk.Button(run, text="Open folder", command=self.open_output)
        self.open_btn.pack(side="right", padx=(6, 0))
        self.run_btn = ttk.Button(run, text="Upscale", command=self.start)
        self.run_btn.pack(side="right", padx=(6, 0))

        root.after(100, self.poll)

    # ----- engine -----
    def refresh_engine(self):
        self.binary = core.engine_binary()
        if self.binary:
            self.engine_label.config(text="AI engine ready (Real-ESRGAN)", foreground="green")
            self.install_btn.pack_forget()
        else:
            self.engine_label.config(text="AI engine not installed. Basic (non-AI) mode only.", foreground="#b45309")
            self.install_btn.pack(side="right")

    def install_engine(self):
        self.install_btn.config(state="disabled", text="Downloading (~50 MB)...")

        def work():
            r = subprocess.run([sys.executable, str(APP_DIR / "setup_engine.py")], capture_output=True, text=True)
            self.events.put(("engine", r.returncode, (r.stderr or r.stdout)[-300:]))

        threading.Thread(target=work, daemon=True).start()

    # ----- file list -----
    def pick_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose images", filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")])
        self.add_files(paths)

    def add_files(self, paths):
        if self.busy:
            return
        skipped = 0
        for p in paths:
            p = str(Path(p))
            if p in self.files or not core.is_supported(p):
                skipped += 1
                continue
            if len(self.files) >= core.MAX_BATCH:
                messagebox.showinfo("Batch full", f"Only {core.MAX_BATCH} images per batch. Extra files were left out.")
                break
            try:
                with Image.open(p) as im:
                    size = im.size
            except Exception:
                skipped += 1
                continue
            self.files.append(p)
            self.tree.insert("", "end", iid=p, text=Path(p).name,
                             values=(f"{size[0]}x{size[1]}", "", "Ready"))
        self.refresh_sizes()

    def refresh_sizes(self):
        s = self.scale.get()
        for p in self.files:
            w, h = (int(v) for v in self.tree.set(p, "size").split("x"))
            self.tree.set(p, "result", f"{w * s}x{h * s}")

    def remove_selected(self):
        if self.busy:
            return
        for iid in self.tree.selection():
            self.files.remove(iid)
            self.tree.delete(iid)

    def clear(self):
        if self.busy:
            return
        self.files.clear()
        self.tree.delete(*self.tree.get_children())
        self.progress["value"] = 0

    def pick_out_dir(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)

    def open_output(self):
        d = Path(self.out_dir.get())
        d.mkdir(parents=True, exist_ok=True)
        core.open_folder(d)

    # ----- run -----
    def start(self):
        if self.busy or not self.files:
            return
        if not self.binary and not messagebox.askyesno(
                "No AI engine", "The AI engine isn't installed, so this will use basic resizing "
                                "(much softer results). Continue anyway?"):
            return
        self.busy = True
        self.run_btn.config(state="disabled")
        self.progress.config(maximum=len(self.files), value=0)
        for p in self.files:
            self.tree.set(p, "status", "Waiting")

        args = dict(files=list(self.files), out_dir=self.out_dir.get(), scale=self.scale.get(),
                    model_key=self.model_keys[self.model.get()], fmt=self.fmt.get(),
                    dpi=300 if self.dpi.get() else None, binary=self.binary)

        def work():
            try:
                core.upscale_batch(**args, on_progress=lambda i, f, st, d: self.events.put(("file", f, st, d)))
            finally:
                self.events.put(("batch_done",))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "file":
                    _, f, status, detail = ev
                    label = {"working": "Upscaling...", "done": "Done", "error": "Failed"}[status]
                    self.tree.set(f, "status", label)
                    if status != "working":
                        self.progress["value"] = float(self.progress["value"]) + 1
                    if status == "error":
                        messagebox.showerror("Upscale failed", f"{Path(f).name}\n\n{detail}")
                elif ev[0] == "batch_done":
                    self.busy = False
                    self.run_btn.config(state="normal")
                elif ev[0] == "engine":
                    _, code, msg = ev
                    self.install_btn.config(state="normal", text="Install AI engine")
                    if code != 0:
                        messagebox.showerror("Install failed", msg)
                    self.refresh_engine()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
