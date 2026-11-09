"""Janela simples que mostra a explicação da build. Roda em processo separado e
relê o arquivo sempre que ele muda.  Uso: python -m coach.build.window ARQUIVO"""
import sys
import tkinter as tk
from pathlib import Path


def _already_open() -> bool:
    """Uma janela só: se outra já está aberta, ela mesma vai mostrar o arquivo novo."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _already_open.handle = k32.CreateMutexW(None, False, "CoachLoLBuildWindow")  # vive com o processo
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    if _already_open():
        return
    path = Path(sys.argv[1])
    root = tk.Tk()
    root.title("Coach LoL — Build calculada")
    root.geometry("980x640")
    root.configure(bg="#14161a")
    top = tk.BooleanVar(value=True)
    root.attributes("-topmost", True)
    bar = tk.Frame(root, bg="#14161a")
    bar.pack(fill="x")
    tk.Checkbutton(bar, text="Sempre no topo", variable=top, bg="#14161a", fg="#c8ccd4", selectcolor="#2a2e36",
                   activebackground="#14161a", command=lambda: root.attributes("-topmost", top.get())).pack(side="left", padx=8)
    status = tk.Label(bar, text="aguardando a primeira recomendação...", bg="#14161a", fg="#7f8796")
    status.pack(side="right", padx=8)
    text = tk.Text(root, wrap="word", bg="#1b1e24", fg="#e3e6ec", insertbackground="#e3e6ec",
                   font=("Consolas", 10), padx=12, pady=10, relief="flat")
    text.pack(fill="both", expand=True)
    state = {"mtime": 0.0}

    def refresh() -> None:
        try:
            m = path.stat().st_mtime
            if m != state["mtime"]:
                state["mtime"] = m
                text.config(state="normal")
                text.delete("1.0", "end")
                text.insert("1.0", path.read_text(encoding="utf-8"))
                text.config(state="disabled")
                status.config(text=f"atualizado: {path.name}")
        except FileNotFoundError:
            pass
        root.after(1000, refresh)

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
