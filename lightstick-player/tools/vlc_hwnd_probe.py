"""诊断: libVLC 在有/无父窗口句柄、不同 vout 下的表现。"""
import os, sys, time
import tkinter as tk
import vlc

clip = sys.argv[1]

def try_case(name, args, make_hwnd):
    inst = vlc.Instance(args)
    mp = inst.media_player_new()
    mp.set_media(inst.media_new(clip))
    hwnd = make_hwnd()
    if hwnd is not None:
        mp.set_hwnd(hwnd)
    mp.play()
    ok = False
    for _ in range(30):
        time.sleep(0.2)
        if mp.get_time() > 200:
            ok = True
            break
    # 位置推进 + 是否有画面输出(通过 vout 计数判断)
    n_vout = mp.video_get_size(0)[0] if hasattr(mp, "video_get_size") else -1
    print(f"{name:34s} time={mp.get_time():5d} tracks_ok={ok} vout_w={n_vout}")
    mp.stop()
    return ok

root = tk.Tk()
root.geometry("200x120+10+10")
root.update()
frame = tk.Frame(root, bg="black", width=100, height=60)
frame.pack()
top = tk.Toplevel(root)
top.geometry("200x120+300+10")
top.update()
root.update()

cases = [
    ("own-window (no hwnd)", [], None),
    ("Tk root hwnd", [], lambda: root.winfo_id()),
    ("Tk toplevel hwnd", [], lambda: top.winfo_id()),
    ("Tk frame hwnd", [], lambda: frame.winfo_id()),
    ("Tk frame hwnd +wingdi", ["--vout=wingdi"], lambda: frame.winfo_id()),
    ("Tk frame hwnd +direct3d11", ["--vout=direct3d11"], lambda: frame.winfo_id()),
]
for name, args, mk in cases:
    try:
        try_case(name, args, mk)
    except Exception as e:
        print(f"{name:34s} EXC {e}")
root.destroy()
