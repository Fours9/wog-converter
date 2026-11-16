"""Desktop interface for the World of Guns cache converter."""
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, ttk

from wog_cache_converter import recover


CACHE = Path.home() / "AppData/LocalLow/Unity/Noble Empire_World of Guns"


def main():
    root = tk.Tk()
    root.title("World of Guns — конвертер кэша")
    root.minsize(660, 360)
    root.columnconfigure(0, weight=1)

    panel = ttk.Frame(root, padding=24)
    panel.grid(sticky="nsew")
    panel.columnconfigure(0, weight=1)
    ttk.Label(panel, text="Конвертер кэша World of Guns",
              font=("Segoe UI", 18, "bold")).grid(sticky="w", pady=(0, 16))

    source = tk.StringVar()
    destination = tk.StringVar(value=str(Path(__file__).resolve().parent))
    key = tk.StringVar()
    selection = tk.StringVar()
    status = tk.StringVar(value="Выбери запись из кэша или укажи файл вручную.")
    messages = Queue()
    cache_files = {}

    def refresh():
        cache_files.clear()
        for path in sorted(CACHE.glob("*/*/__data")):
            cache_files[f"{path.parent.parent.name} / {path.parent.name[:8]}"] = path
        picker.configure(values=tuple(cache_files))
        if not source.get() and cache_files:
            first = next(iter(cache_files))
            selection.set(first)
            use_selection()

    def use_selection(event=None):
        path = cache_files.get(selection.get())
        if path:
            source.set(str(path))

    def browse_source():
        path = filedialog.askopenfilename(title="Выбери __data или .bytes",
                                          initialdir=str(CACHE) if CACHE.is_dir() else None)
        if path:
            selection.set("")
            source.set(path)

    def browse_destination():
        path = filedialog.askdirectory(title="Куда сохранить .unity3d",
                                       initialdir=destination.get())
        if path:
            destination.set(path)

    ttk.Label(panel, text="Запись в кэше").grid(sticky="w")
    picker = ttk.Combobox(panel, state="readonly", textvariable=selection)
    picker.grid(row=2, column=0, sticky="ew", padx=(0, 8))
    picker.bind("<<ComboboxSelected>>", use_selection)
    ttk.Button(panel, text="Обновить", command=refresh).grid(row=2, column=1, sticky="ew")

    for row, label, variable, action in [
        (3, "Исходный файл", source, browse_source),
        (5, "Папка сохранения", destination, browse_destination),
    ]:
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=(12, 2))
        ttk.Entry(panel, textvariable=variable).grid(row=row + 1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(panel, text="Обзор…", command=action).grid(row=row + 1, column=1, sticky="ew")

    ttk.Label(panel, text="Ключ (пустое поле — автоподбор, ручной ввод — 32 символа HEX)").grid(
        row=7, column=0, sticky="w", pady=(12, 2))
    ttk.Entry(panel, textvariable=key, show="•").grid(row=8, column=0, sticky="ew", padx=(0, 8))
    ttk.Label(panel, textvariable=status, wraplength=590).grid(
        row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def convert_file(path, folder, active_key):
        try:
            data = recover(path.read_bytes(), active_key)
            name = path.parent.parent.name if path.name == "__data" else path.stem
            for index in range(1000):
                suffix = f"_{index}" if index else ""
                output = folder / f"{name}{suffix}.unity3d"
                try:
                    with output.open("xb") as stream:
                        stream.write(data)
                    messages.put((True, output))
                    return
                except FileExistsError:
                    continue
            raise OSError("Слишком много файлов с таким именем.")
        except Exception as error:
            detail = str(error)
            if detail.startswith("No valid inner bundle"):
                detail = "Не удалось подтвердить внутренний UnityFS-бандл. Проверь файл или укажи известный ключ."
            messages.put((False, detail))

    def poll():
        try:
            ok, result = messages.get_nowait()
        except Empty:
            root.after(100, poll)
            return
        convert.configure(state="normal")
        if ok:
            status.set(f"Готово. Файл для AssetRipper: {result}")
            open_folder.configure(state="normal", command=lambda: os.startfile(result.parent))
        else:
            status.set(f"Не удалось конвертировать. {result}")

    def start():
        path = Path(source.get().strip())
        folder = Path(destination.get().strip())
        active_key = key.get().strip()
        if not path.is_file() or not destination.get().strip() or not folder.is_dir():
            status.set("Выбери существующий исходный файл и папку для сохранения.")
            return
        if active_key and (len(active_key) != 32 or
                           any(c not in "0123456789abcdefABCDEF" for c in active_key)):
            status.set("Ключ должен содержать ровно 32 шестнадцатеричных символа.")
            return
        convert.configure(state="disabled")
        open_folder.configure(state="disabled")
        status.set("Читаю кэш, подбираю ключ и проверяю бандл…" if not active_key
                   else "Проверяю бандл с указанным ключом…")
        Thread(target=convert_file, args=(path, folder, active_key.encode() or None),
               daemon=True).start()
        root.after(100, poll)

    convert = ttk.Button(panel, text="Конвертировать", command=start)
    convert.grid(row=9, column=0, sticky="w", pady=(16, 0))
    open_folder = ttk.Button(panel, text="Открыть папку", state="disabled")
    open_folder.grid(row=9, column=1, sticky="ew", pady=(16, 0))
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
