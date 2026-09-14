"""最小 libVLC 探测: 不嵌窗口, 看本机 VLC 能否解码并推进时钟。"""
import os, sys, time
os.environ.setdefault("VLC_VERBOSE", "-1")
import vlc

clip = sys.argv[1]
inst = vlc.Instance()
print("inst:", inst)
mp = inst.media_player_new()
mp.set_media(inst.media_new(clip))
print("play ->", mp.play())
for i in range(40):
    time.sleep(0.25)
    st = mp.get_state()
    print(i, "state=", st, "t=", mp.get_time(), "len=", mp.get_length(), "playing=", mp.is_playing())
    if st == vlc.State.Ended:
        break
mp.stop()
